from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta
from typing import Any, Callable, Dict, Optional

from flask import Flask, current_app, has_app_context
from sqlalchemy import func
from sqlalchemy.exc import OperationalError, ProgrammingError
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from ..extensions import db
from ..models import ApiToken, ApiTokenReveal, BroadcastMessage, JobQueue, JobRun, LogEvent, User, UserMessage, now_utc

log = logging.getLogger(__name__)

JobHandler = Callable[[Flask, int], None]
TERMINAL_JOB_STATUSES = ("completed", "failed", "stopped")
QUEUE_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,39}$")
JOB_TYPE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,79}$")
MAX_JOB_PAYLOAD_BYTES = 32 * 1024
MAX_JOB_RETRIES = 10
MAX_JOB_TIMEOUT_SEC = 3600.0
MAX_JOB_BACKOFF_SEC = 120.0
REDIS_BACKEND = "redis"
DB_BACKEND = "db"


def _log_job_event(event: str, **context: Any) -> None:
    try:
        log.info(
            "job.%s",
            event,
            extra={"context": {k: v for k, v in context.items() if v is not None}},
        )
    except Exception:
        return


def _default_queue_concurrency_from_cfg(cfg: Any) -> int:
    try:
        val = int((cfg or {}).get("JOB_QUEUE_DEFAULT_CONCURRENCY", 2))
    except Exception:
        val = 2
    return max(1, min(64, val))


class _Runtime:
    def __init__(self, app: Flask):
        self.app = app
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.handlers: Dict[str, JobHandler] = {}
        self.handler_meta: Dict[str, Dict[str, Any]] = {}
        self.inflight_job_ids: set[int] = set()
        self.poll_interval_sec = float(app.config.get("JOB_RUNTIME_POLL_SEC", 0.2))
        self.backend = str(app.config.get("JOB_QUEUE_BACKEND", DB_BACKEND)).strip().lower()
        if self.backend not in (DB_BACKEND, REDIS_BACKEND):
            self.backend = DB_BACKEND
        self.redis_url = str(app.config.get("JOB_QUEUE_REDIS_URL", "")).strip()
        self.redis_namespace = str(app.config.get("JOB_QUEUE_REDIS_NAMESPACE", "flaskbase")).strip() or "flaskbase"
        self.redis_list_timeout = max(1, min(int(app.config.get("JOB_QUEUE_REDIS_BLOCKING_TIMEOUT_SEC", 2)), 30))
        self._redis: Redis | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._worker_loop, name="job-worker", daemon=True)
        self.thread.start()

    def wake(self) -> None:
        self.wake_event.set()

    def _worker_loop(self) -> None:
        _log_job_event("runtime.started", poll_interval_sec=self.poll_interval_sec, backend=self.backend)
        if self.backend == REDIS_BACKEND and self._redis_client() is not None:
            self._worker_loop_redis()
            _log_job_event("runtime.stopped", backend=self.backend)
            return
        if self.backend == REDIS_BACKEND:
            _log_job_event("runtime.backend_fallback", requested_backend=REDIS_BACKEND, effective_backend=DB_BACKEND)
        while not self.stop_event.is_set():
            try:
                with self.app.app_context():
                    self._tick()
            except Exception:
                log.exception("job.runtime.tick_failed")
            self.wake_event.wait(timeout=self.poll_interval_sec)
            self.wake_event.clear()
        _log_job_event("runtime.stopped", backend=self.backend)

    def _redis_client(self) -> Redis | None:
        if self.backend != REDIS_BACKEND:
            return None
        if not self.redis_url:
            return None
        if self._redis is not None:
            return self._redis
        try:
            # BLPOP blocks up to redis_list_timeout seconds, so socket timeout must be higher.
            read_timeout = max(5.0, float(self.redis_list_timeout) + 3.0)
            client = Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_timeout=read_timeout,
                socket_connect_timeout=2.0,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            client.ping()
            self._redis = client
            return client
        except Exception:
            self._redis = None
            return None

    def _redis_queue_list_key(self, queue_key: str) -> str:
        return f"{self.redis_namespace}:jobs:queue:{queue_key}"

    def _redis_queue_keys(self) -> list[str]:
        queues = JobQueue.query.filter_by(enabled=True, paused=False).order_by(JobQueue.queue_key.asc()).all()
        return [self._redis_queue_list_key(str(q.queue_key)) for q in queues]

    def _redis_enqueue(self, queue_key: str, job_id: int, *, left: bool = False) -> bool:
        client = self._redis_client()
        if client is None:
            return False
        list_key = self._redis_queue_list_key(queue_key)
        try:
            if left:
                client.lpush(list_key, str(int(job_id)))
            else:
                client.rpush(list_key, str(int(job_id)))
            return True
        except Exception:
            return False

    def _worker_loop_redis(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self.app.app_context():
                    queue_keys = self._redis_queue_keys()
                client = self._redis_client()
                if client is None or not queue_keys:
                    self.wake_event.wait(timeout=self.poll_interval_sec)
                    self.wake_event.clear()
                    continue
                popped = client.blpop(queue_keys, timeout=self.redis_list_timeout)
                if not popped:
                    continue
                list_key, raw_job_id = popped
                queue_key = str(list_key).rsplit(":", 1)[-1]
                try:
                    job_id = int(str(raw_job_id).strip())
                except Exception:
                    continue
                with self.app.app_context():
                    if self._mark_job_running(job_id, enforce_capacity=True):
                        self._spawn_job(job_id)
                    else:
                        # If still queued (e.g. queue concurrency reached), put it back.
                        job = db.session.get(JobRun, job_id, populate_existing=True)
                        if job and str(job.status or "") == "queued":
                            self._redis_enqueue(queue_key, job_id)
            except (RedisTimeoutError, RedisConnectionError):
                # Network hiccup or read timeout during BLPOP: reconnect and continue.
                self._redis = None
                self.wake_event.wait(timeout=min(1.0, max(0.05, self.poll_interval_sec)))
                self.wake_event.clear()
            except Exception:
                log.exception("job.runtime.redis_tick_failed")
                self._redis = None
                time.sleep(min(2.0, max(0.05, self.poll_interval_sec)))

    def _tick(self) -> None:
        if not self.handlers:
            return

        # If the DB was reset/recreated while the runtime is running (common in dev),
        # the tables may temporarily not exist. Self-heal by recreating job tables.
        try:
            queues = JobQueue.query.filter_by(enabled=True, paused=False).all()
        except (ProgrammingError, OperationalError):
            try:
                ensure_job_tables(self.app)
            except Exception:
                pass
            return
        for queue in queues:
            concurrency = max(1, int(queue.concurrency or 1))
            running = JobRun.query.filter_by(queue_id=queue.id, status="running").count()
            slots = max(0, concurrency - running)
            if slots <= 0:
                continue

            queued = (
                JobRun.query.filter_by(queue_id=queue.id, status="queued")
                .order_by(JobRun.created_at.asc())
                .limit(slots)
                .all()
            )
            for job in queued:
                if self._mark_job_running(job.id, enforce_capacity=False):
                    self._spawn_job(job.id)

    def _mark_job_running(self, job_id: int, *, enforce_capacity: bool = False) -> bool:
        job = _get_job(job_id, fresh=True)
        if not job or job.status != "queued":
            return False
        queue = job.queue
        if queue is not None:
            if bool(getattr(queue, "paused", False)) or not bool(getattr(queue, "enabled", True)):
                return False
            if enforce_capacity:
                concurrency = max(1, int(queue.concurrency or 1))
                running = JobRun.query.filter_by(queue_id=queue.id, status="running").count()
                if running >= concurrency:
                    return False
        job.status = "running"
        job.started_at = now_utc()
        job.heartbeat_at = now_utc()
        db.session.commit()
        _log_job_event(
            "started",
            job_id=job.id,
            queue_id=job.queue_id,
            job_type=job.job_type,
            requested_by_user_id=job.requested_by_user_id,
            attempt=_runtime_attempt_from_result(job.result) + 1,
        )
        return True

    def _spawn_job(self, job_id: int) -> None:
        with self.lock:
            if job_id in self.inflight_job_ids:
                return
            self.inflight_job_ids.add(job_id)
        t = threading.Thread(target=self._run_job_thread, args=(job_id,), daemon=True, name=f"job-{job_id}")
        t.start()

    def _run_job_thread(self, job_id: int) -> None:
        try:
            with self.app.app_context():
                job = db.session.get(JobRun, job_id, populate_existing=True)
                if not job:
                    return
                handler = self.handlers.get(job.job_type)
                if handler is None:
                    job.status = "failed"
                    job.message = f"Unknown job type: {job.job_type}"
                    job.finished_at = now_utc()
                    job.heartbeat_at = now_utc()
                    db.session.commit()
                    _log_job_event("failed", job_id=job_id, reason="missing_handler", job_type=job.job_type)
                    return

                meta = dict(self.handler_meta.get(job.job_type) or {})
                max_retries = _clamp_int(meta.get("max_retries", 0), 0, MAX_JOB_RETRIES)
                timeout_sec = _clamp_float(meta.get("timeout_sec", 0.0), 0.0, MAX_JOB_TIMEOUT_SEC)
                retry_backoff_sec = _clamp_float(meta.get("retry_backoff_sec", 0.0), 0.0, MAX_JOB_BACKOFF_SEC)

                # Track retry attempt in job.result._runtime meta without touching user payload.
                attempt = _runtime_attempt_from_result(job.result) + 1
                _set_runtime_meta(job, attempt=attempt, max_retries=max_retries, timeout_sec=timeout_sec)
                db.session.commit()
                _log_job_event(
                    "attempt.started",
                    job_id=job_id,
                    job_type=job.job_type,
                    attempt=attempt,
                    max_retries=max_retries,
                    timeout_sec=timeout_sec,
                )

                try:
                    timed_out, err = _run_handler_with_timeout(handler, self.app, job_id, timeout_sec=timeout_sec)
                    if err is not None:
                        raise err
                    if timed_out:
                        raise TimeoutError(f"Job timeout after {timeout_sec:.1f}s")
                    db.session.expire_all()
                    final = db.session.get(JobRun, job_id, populate_existing=True)
                    if not final:
                        return
                    if final.stop_requested and final.status not in ("stopped", "failed", "completed"):
                        final.status = "stopped"
                        final.message = final.message or "Stopped by request"
                    elif final.status == "running":
                        final.status = "completed"
                        final.progress = 100
                        final.message = final.message or "Completed"
                    if final.status in ("completed", "failed", "stopped"):
                        final.finished_at = final.finished_at or now_utc()
                    final.heartbeat_at = now_utc()
                    db.session.commit()
                    _log_job_event(
                        "completed" if final.status == "completed" else final.status,
                        job_id=job_id,
                        job_type=final.job_type,
                        attempt=attempt,
                        progress=final.progress,
                        message=final.message,
                    )
                except Exception as ex:
                    db.session.rollback()
                    failed = db.session.get(JobRun, job_id, populate_existing=True)
                    retried = False
                    if failed:
                        retried = _try_requeue_failed_job(
                            failed,
                            attempt=attempt,
                            max_retries=max_retries,
                            retry_backoff_sec=retry_backoff_sec,
                            error=str(ex),
                        )
                        if not retried:
                            failed.status = "failed"
                            failed.message = str(ex)[:250]
                            failed.finished_at = now_utc()
                            failed.heartbeat_at = now_utc()
                            _set_runtime_meta(
                                failed,
                                attempt=attempt,
                                max_retries=max_retries,
                                timeout_sec=timeout_sec,
                                last_error=str(ex),
                            )
                        db.session.commit()
                    _log_job_event(
                        "failed",
                        job_id=job_id,
                        attempt=attempt,
                        job_type=(failed.job_type if failed else None),
                        error=str(ex)[:180],
                        retried=bool(retried),
                    )
                    if retried:
                        log.warning("job.runtime.job_failed_requeued | job_id=%s | error=%s", job_id, str(ex)[:180])
                    else:
                        log.exception("job.runtime.job_failed | job_id=%s", job_id)
        finally:
            with self.lock:
                self.inflight_job_ids.discard(job_id)
            self.wake()
            try:
                db.session.remove()
            except Exception:
                pass


def _get_job(job_id: int, fresh: bool = False) -> Optional[JobRun]:
    if fresh:
        return db.session.get(JobRun, job_id, populate_existing=True)
    return db.session.get(JobRun, job_id)


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        out = int(value)
    except Exception:
        out = low
    return max(low, min(high, out))


def _clamp_float(value: Any, low: float, high: float) -> float:
    try:
        out = float(value)
    except Exception:
        out = low
    return max(low, min(high, out))


def _runtime_attempt_from_result(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    rt = result.get("_runtime")
    if not isinstance(rt, dict):
        return 0
    return _clamp_int(rt.get("attempt", 0), 0, MAX_JOB_RETRIES + 1)


def _set_runtime_meta(
    job: JobRun,
    *,
    attempt: int,
    max_retries: int,
    timeout_sec: float,
    last_error: str | None = None,
) -> None:
    base = dict(job.result or {}) if isinstance(job.result, dict) else {}
    rt = dict(base.get("_runtime") or {})
    rt["attempt"] = int(attempt)
    rt["max_retries"] = int(max_retries)
    rt["timeout_sec"] = float(timeout_sec)
    if last_error:
        rt["last_error"] = str(last_error)[:250]
        rt["last_failed_at"] = now_utc().isoformat() + "Z"
    base["_runtime"] = rt
    job.result = base


def _run_handler_with_timeout(
    handler: JobHandler,
    app: Flask,
    job_id: int,
    *,
    timeout_sec: float,
) -> tuple[bool, Exception | None]:
    """Execute a handler and optionally enforce timeout with cooperative stop flag."""
    if timeout_sec <= 0.0:
        try:
            handler(app, job_id)
            return (False, None)
        except Exception as ex:
            return (False, ex)

    err_holder: list[Exception] = []
    done = threading.Event()

    def _target() -> None:
        with app.app_context():
            try:
                handler(app, job_id)
            except Exception as ex:
                err_holder.append(ex)
            finally:
                done.set()

    t = threading.Thread(target=_target, daemon=True, name=f"job-handler-{job_id}")
    t.start()
    t.join(timeout=timeout_sec)
    if done.is_set():
        return (False, err_holder[0] if err_holder else None)

    # Cooperative timeout: mark stop request; handlers that poll control can exit.
    timed = _get_job(job_id, fresh=True)
    if timed is not None:
        timed.stop_requested = True
        timed.heartbeat_at = now_utc()
        db.session.commit()
    return (True, None)


def _try_requeue_failed_job(
    job: JobRun,
    *,
    attempt: int,
    max_retries: int,
    retry_backoff_sec: float,
    error: str,
) -> bool:
    if attempt > max_retries:
        return False
    if retry_backoff_sec > 0:
        time.sleep(min(retry_backoff_sec, MAX_JOB_BACKOFF_SEC))
    job.status = "queued"
    job.stop_requested = False
    job.progress = min(int(job.progress or 0), 95)
    job.started_at = None
    job.finished_at = None
    job.message = f"Retry {attempt}/{max_retries}: {str(error)[:160]}"
    job.heartbeat_at = now_utc()
    _set_runtime_meta(
        job,
        attempt=attempt,
        max_retries=max_retries,
        timeout_sec=_clamp_float(
            (dict(job.result or {}).get("_runtime") or {}).get("timeout_sec", 0.0),
            0.0,
            MAX_JOB_TIMEOUT_SEC,
        ),
        last_error=error,
    )
    _log_job_event(
        "requeued",
        job_id=job.id,
        job_type=job.job_type,
        attempt=attempt,
        max_retries=max_retries,
        retry_backoff_sec=retry_backoff_sec,
        error=str(error)[:180],
    )
    return True


def _payload_dict(job_id: int) -> Dict[str, Any]:
    job = _get_job(job_id)
    payload = (job.payload if job else {}) or {}
    return payload if isinstance(payload, dict) else {}


def _job_update(job_id: int, commit: bool = True, **kwargs: Any) -> Optional[JobRun]:
    job = _get_job(job_id)
    if not job:
        return None
    for key, value in kwargs.items():
        setattr(job, key, value)
    job.heartbeat_at = now_utc()
    if commit:
        db.session.commit()
    return job


def _job_control(job_id: int) -> tuple[str, bool]:
    job = _get_job(job_id, fresh=True)
    if not job:
        return ("missing", True)
    return (str(job.status or ""), bool(job.stop_requested))


def _wait_while_paused(job_id: int) -> bool:
    while True:
        status, stop_requested = _job_control(job_id)
        if stop_requested:
            _job_update(job_id, status="stopped", message="Stopped while paused")
            return False
        if status != "paused":
            return True
        time.sleep(0.1)


def _simulate_delay_job(app: Flask, job_id: int) -> None:
    payload = _payload_dict(job_id)
    steps = max(1, min(int(payload.get("steps", 16)), 1000))
    step_ms = max(5, min(int(payload.get("step_ms", 120)), 5000))

    for idx in range(1, steps + 1):
        status, stop_requested = _job_control(job_id)
        if stop_requested:
            _job_update(job_id, status="stopped", message="Stopped by request")
            return
        if status == "paused" and not _wait_while_paused(job_id):
            return

        progress = int((idx / steps) * 100)
        _job_update(job_id, message=f"Step {idx}/{steps}", progress=progress)
        time.sleep(step_ms / 1000.0)


def _log_batch_job(app: Flask, job_id: int) -> None:
    payload = _payload_dict(job_id)
    total = max(1, min(int(payload.get("events", 2000)), 50000))
    level = str(payload.get("level", "INFO")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        level = "INFO"

    batch_size = max(20, min(int(payload.get("batch_size", 500)), 5000))
    control_check_every = max(5, min(int(payload.get("control_check_every", 100)), 5000))
    buffer: list[LogEvent] = []
    owner_id = None
    owner_job = _get_job(job_id)
    if owner_job:
        owner_id = owner_job.requested_by_user_id

    for idx in range(1, total + 1):
        if idx % control_check_every == 0 or idx == 1:
            status, stop_requested = _job_control(job_id)
            if stop_requested:
                if buffer:
                    db.session.add_all(buffer)
                    db.session.commit()
                _job_update(job_id, status="stopped", message=f"Stopped at {idx-1}/{total}")
                return
            if status == "paused" and not _wait_while_paused(job_id):
                return

        buffer.append(
            LogEvent(
                level=level,
                event_type=f"job.log_batch.{level.lower()}",
                message=f"Job #{job_id} event {idx}/{total}",
                user_id=owner_id,
                context={"job_id": job_id, "seq": idx, "total": total, "source": "job.log_batch"},
            )
        )

        if len(buffer) >= batch_size or idx == total:
            db.session.add_all(buffer)
            buffer = []
            job = _get_job(job_id, fresh=True)
            if not job:
                return
            job.progress = int((idx / total) * 100)
            job.message = f"Logged {idx}/{total}"
            job.heartbeat_at = now_utc()
            db.session.commit()


def _purge_old_logs_job(app: Flask, job_id: int) -> None:
    payload = _payload_dict(job_id)
    days = max(1, min(int(payload.get("days", 90)), 3650))
    cutoff = now_utc() - timedelta(days=days)
    deleted = LogEvent.query.filter(LogEvent.ts < cutoff).delete(synchronize_session=False)
    job = _get_job(job_id)
    if not job:
        db.session.commit()
        return
    job.result = {"days": days, "deleted": int(deleted)}
    job.progress = 100
    job.message = f"Deleted {deleted} logs older than {days} days"
    job.heartbeat_at = now_utc()
    db.session.commit()


def _user_metrics_snapshot_job(app: Flask, job_id: int) -> None:
    total = User.query.count()
    active = User.query.filter_by(is_active=True).count()
    admins = User.query.filter_by(role="admin").count()
    per_role = {
        role: int(cnt)
        for role, cnt in db.session.query(User.role, func.count(User.id)).group_by(User.role).all()
    }

    top_emitters = (
        db.session.query(User.email, func.count(LogEvent.id).label("events"))
        .join(LogEvent, LogEvent.user_id == User.id)
        .group_by(User.email)
        .order_by(func.count(LogEvent.id).desc())
        .limit(10)
        .all()
    )

    job = _get_job(job_id)
    if not job:
        return
    job.result = {
        "total_users": int(total),
        "active_users": int(active),
        "admin_users": int(admins),
        "per_role": per_role,
        "top_emitters": [{"email": e, "events": int(c)} for e, c in top_emitters],
    }
    job.progress = 100
    job.message = "User metrics snapshot completed"
    job.heartbeat_at = now_utc()
    db.session.commit()


def _token_health_report_job(app: Flask, job_id: int) -> None:
    payload = _payload_dict(job_id)
    days = max(1, min(int(payload.get("expiring_days", 14)), 365))
    now = now_utc()
    cutoff = now + timedelta(days=days)

    total_tokens = ApiToken.query.count()
    revoked_tokens = ApiToken.query.filter(ApiToken.revoked_at.isnot(None)).count()
    active_tokens = ApiToken.query.filter(ApiToken.revoked_at.is_(None)).count()
    expiring_soon = (
        ApiToken.query.filter(ApiToken.revoked_at.is_(None), ApiToken.expires_at.isnot(None), ApiToken.expires_at <= cutoff).count()
    )
    pending_reveals = ApiTokenReveal.query.filter(ApiTokenReveal.revealed_at.is_(None)).count()

    job = _get_job(job_id)
    if not job:
        return
    job.result = {
        "total_tokens": int(total_tokens),
        "active_tokens": int(active_tokens),
        "revoked_tokens": int(revoked_tokens),
        "expiring_within_days": int(expiring_soon),
        "days": days,
        "pending_reveals": int(pending_reveals),
    }
    job.progress = 100
    job.message = "Token health report ready"
    job.heartbeat_at = now_utc()
    db.session.commit()


def _send_email_payload_job(app: Flask, job_id: int) -> None:
    from .email_service import send_email

    payload = _payload_dict(job_id)
    send_email(
        to_email=str(payload.get("to_email") or "").strip(),
        subject=str(payload.get("subject") or "").strip(),
        text_body=str(payload.get("text_body") or ""),
        html_body=(str(payload.get("html_body")) if payload.get("html_body") else None),
    )
    job = _get_job(job_id)
    if not job:
        return
    job.result = {"status": "sent", "to_email": str(payload.get("to_email") or "").strip().lower()}
    job.progress = 100
    job.message = "Email sent"
    job.heartbeat_at = now_utc()
    db.session.commit()


def _deliver_user_message_email_job(app: Flask, job_id: int) -> None:
    from .message_delivery_service import send_message_email

    payload = _payload_dict(job_id)
    message_id = int(payload.get("message_id", 0) or 0)
    row = db.session.get(UserMessage, message_id)
    if row is None or row.user is None:
        raise RuntimeError(f"user message not found: {message_id}")
    send_message_email(
        recipient=row.user,
        title=row.title,
        body=row.body,
        body_format=row.body_format,
        level=row.level,
        template_key=row.email_template_key,
        subject=row.email_subject or row.title,
        preheader=row.email_preheader,
        action_label=row.action_label,
        action_url=row.action_url,
    )
    row.email_sent_at = now_utc()
    row.email_error = None
    job = _get_job(job_id)
    if job:
        job.result = {"status": "sent", "message_id": message_id, "user_id": int(row.user_id or 0)}
        job.progress = 100
        job.message = "User message email sent"
        job.heartbeat_at = now_utc()
    db.session.commit()


def _deliver_broadcast_email_batch_job(app: Flask, job_id: int) -> None:
    from .message_delivery_service import send_message_email

    payload = _payload_dict(job_id)
    broadcast_id = int(payload.get("broadcast_id", 0) or 0)
    row = db.session.get(BroadcastMessage, broadcast_id)
    if row is None:
        raise RuntimeError(f"broadcast not found: {broadcast_id}")

    recipients = (
        User.query.filter_by(is_active=True)
        .filter(User.notification_email_enabled.is_(True))
        .order_by(User.id.asc())
        .all()
    )
    total = len(recipients)
    sent = 0
    failed = 0
    failure_messages: list[str] = []

    for idx, recipient in enumerate(recipients, start=1):
        status, stop_requested = _job_control(job_id)
        if stop_requested:
            _job_update(job_id, status="stopped", message=f"Stopped at {sent}/{total}", progress=int((sent / max(total, 1)) * 100))
            return
        if status == "paused" and not _wait_while_paused(job_id):
            return

        try:
            send_message_email(
                recipient=recipient,
                title=row.title,
                body=row.body,
                body_format=row.body_format,
                level=row.level,
                template_key=row.email_template_key,
                subject=row.email_subject or row.title,
                preheader=row.email_preheader,
                action_label=row.action_label,
                action_url=row.action_url,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            failure_messages.append(f"{recipient.email}: {str(exc)[:80]}")

        progress = int((idx / max(total, 1)) * 100)
        _job_update(job_id, message=f"Broadcast email {idx}/{total}", progress=progress)

    row.email_sent_at = now_utc() if sent else None
    row.email_error = "; ".join(failure_messages[:3])[:255] if failure_messages else None
    job = _get_job(job_id)
    if job:
        job.result = {"status": "completed", "broadcast_id": broadcast_id, "sent": sent, "failed": failed, "total": total}
        job.progress = 100
        job.message = f"Broadcast email sent to {sent}/{total}"
        job.heartbeat_at = now_utc()
    db.session.commit()


def _get_runtime(app: Flask) -> _Runtime:
    rt = app.extensions.get("job_runtime")
    if rt is None:
        rt = _Runtime(app)
        app.extensions["job_runtime"] = rt
    return rt


def _runtime_backend_from_app(app: Flask) -> str:
    backend = str(app.config.get("JOB_QUEUE_BACKEND", DB_BACKEND)).strip().lower()
    if backend not in (DB_BACKEND, REDIS_BACKEND):
        backend = DB_BACKEND
    return backend


def _runtime_backend() -> str:
    if not has_app_context():
        return DB_BACKEND
    return _runtime_backend_from_app(current_app)


def _redis_enqueue_job(queue_key: str, job_id: int) -> bool:
    if not has_app_context():
        return False
    rt = current_app.extensions.get("job_runtime")
    if rt is None:
        rt = _get_runtime(current_app)
    if not isinstance(rt, _Runtime):
        return False
    return rt._redis_enqueue(queue_key, job_id)


def _redis_sync_queued_jobs(app: Flask, *, limit: int = 5000) -> int:
    if _runtime_backend_from_app(app) != REDIS_BACKEND:
        return 0
    with app.app_context():
        rt = _get_runtime(app)
        if rt._redis_client() is None:
            return 0
        rows = (
            JobRun.query.join(JobQueue, JobQueue.id == JobRun.queue_id)
            .filter(JobRun.status == "queued", JobQueue.enabled.is_(True), JobQueue.paused.is_(False))
            .order_by(JobRun.created_at.asc())
            .limit(max(1, min(int(limit), 20000)))
            .all()
        )
        pushed = 0
        for row in rows:
            qkey = row.queue.queue_key if row.queue else "default"
            if rt._redis_enqueue(str(qkey), int(row.id)):
                pushed += 1
        return pushed


def _wake_runtime() -> None:
    if not has_app_context():
        return
    rt = current_app.extensions.get("job_runtime")
    if rt is not None:
        rt.wake()


def set_job_runtime_poll_interval(seconds: float) -> float:
    """Update scheduler poll interval at runtime and wake worker."""
    if not has_app_context():
        return seconds
    try:
        val = min(5.0, max(0.05, float(seconds)))
    except Exception:
        val = 0.2
    rt = current_app.extensions.get("job_runtime")
    if rt is not None:
        rt.poll_interval_sec = val
        rt.wake()
    current_app.config["JOB_RUNTIME_POLL_SEC"] = val
    return val


def ensure_job_tables(app: Flask) -> None:
    with app.app_context():
        default_concurrency = _default_queue_concurrency_from_cfg(app.config)
        JobQueue.__table__.create(bind=db.engine, checkfirst=True)
        JobRun.__table__.create(bind=db.engine, checkfirst=True)
        default_q = JobQueue.query.filter_by(queue_key="default").first()
        if not default_q:
            db.session.add(
                JobQueue(
                    queue_key="default",
                    name="Default queue",
                    enabled=True,
                    concurrency=default_concurrency,
                    paused=False,
                )
            )
            db.session.commit()
        elif int(default_q.concurrency or 1) < default_concurrency:
            default_q.concurrency = default_concurrency
            db.session.commit()


def init_job_runtime(app: Flask) -> None:
    ensure_job_tables(app)
    rt = _get_runtime(app)

    if not rt.handlers:
        register_job_handler(
            app,
            "simulate_delay",
            _simulate_delay_job,
            timeout_sec=300.0,
            meta={
                "label": "Simulate Delay",
                "description": "Job dimostrativo rapido con step/pause/stop.",
                "example_payload": {"steps": 40, "step_ms": 30},
            },
        )
        register_job_handler(
            app,
            "log_batch",
            _log_batch_job,
            timeout_sec=600.0,
            meta={
                "label": "Log Batch",
                "description": "Genera log in batch ad alta velocita.",
                "example_payload": {"events": 5000, "level": "INFO", "batch_size": 500},
            },
        )
        register_job_handler(
            app,
            "purge_old_logs",
            _purge_old_logs_job,
            timeout_sec=120.0,
            meta={
                "label": "Purge Old Logs",
                "description": "Pulisce log piu vecchi di N giorni.",
                "example_payload": {"days": 90},
            },
        )
        register_job_handler(
            app,
            "user_metrics_snapshot",
            _user_metrics_snapshot_job,
            timeout_sec=120.0,
            meta={
                "label": "User Metrics Snapshot",
                "description": "Calcola metriche utenti e top emitters.",
                "example_payload": {},
            },
        )
        register_job_handler(
            app,
            "token_health_report",
            _token_health_report_job,
            timeout_sec=120.0,
            meta={
                "label": "Token Health Report",
                "description": "Report su token attivi/revocati/in scadenza.",
                "example_payload": {"expiring_days": 14},
            },
        )
        register_job_handler(
            app,
            "send_email_payload",
            _send_email_payload_job,
            timeout_sec=180.0,
            queue_key="email",
            max_retries=2,
            retry_backoff_sec=5.0,
            meta={
                "label": "Send Email Payload",
                "description": "Send a transactional email asynchronously.",
                "example_payload": {"to_email": "user@example.com", "subject": "Hello", "text_body": "Hi"},
            },
        )
        register_job_handler(
            app,
            "deliver_user_message_email",
            _deliver_user_message_email_job,
            timeout_sec=180.0,
            queue_key="email",
            max_retries=2,
            retry_backoff_sec=5.0,
            meta={
                "label": "Deliver User Message Email",
                "description": "Send an email for a direct inbox message.",
                "example_payload": {"message_id": 1},
            },
        )
        register_job_handler(
            app,
            "deliver_broadcast_email_batch",
            _deliver_broadcast_email_batch_job,
            timeout_sec=1800.0,
            queue_key="email",
            max_retries=1,
            retry_backoff_sec=10.0,
            meta={
                "label": "Deliver Broadcast Email Batch",
                "description": "Deliver a broadcast email to all eligible active users.",
                "example_payload": {"broadcast_id": 1},
            },
        )

    if app.config.get("JOB_RUNTIME_AUTOSTART", True):
        if app.debug:
            import os

            if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
                return
        if _runtime_backend_from_app(app) == REDIS_BACKEND:
            restored = _redis_sync_queued_jobs(app, limit=5000)
            if restored:
                _log_job_event("redis.sync_queued", restored=restored)
        rt.start()


def list_job_handlers(app: Flask) -> Dict[str, Dict[str, Any]]:
    rt = _get_runtime(app)
    out: Dict[str, Dict[str, Any]] = {}
    for key, meta in (rt.handler_meta or {}).items():
        item = dict(meta or {})
        item.setdefault("label", key.replace("_", " ").title())
        item.setdefault("description", "No description")
        item.setdefault("example_payload", {})
        item["registered"] = key in rt.handlers
        out[key] = item
    for key in rt.handlers.keys():
        if key not in out:
            out[key] = {
                "label": key.replace("_", " ").title(),
                "description": "Registered job handler",
                "example_payload": {},
                "registered": True,
            }
    return out


def register_job_handler(
    app: Flask,
    job_type: str,
    handler: JobHandler,
    meta: Optional[Dict[str, Any]] = None,
    *,
    queue_key: str = "default",
    max_retries: int = 0,
    retry_backoff_sec: float = 0.0,
    timeout_sec: float = 0.0,
) -> None:
    rt = _get_runtime(app)
    key = (job_type or "").strip()
    if not key:
        raise ValueError("job_type obbligatorio")
    rt.handlers[key] = handler
    h_meta = dict(meta or {})
    h_meta.setdefault("label", key.replace("_", " ").title())
    h_meta.setdefault("description", "Registered job handler")
    h_meta.setdefault("example_payload", {})
    h_meta["queue_key"] = str(queue_key or "default").strip()[:40] or "default"
    h_meta["max_retries"] = _clamp_int(max_retries, 0, MAX_JOB_RETRIES)
    h_meta["retry_backoff_sec"] = _clamp_float(retry_backoff_sec, 0.0, MAX_JOB_BACKOFF_SEC)
    h_meta["timeout_sec"] = _clamp_float(timeout_sec, 0.0, MAX_JOB_TIMEOUT_SEC)
    rt.handler_meta[key] = h_meta
    rt.wake()


def list_queues() -> list[JobQueue]:
    return JobQueue.query.order_by(JobQueue.queue_key.asc()).all()


def list_jobs(limit: int = 100) -> list[JobRun]:
    limit = max(1, min(int(limit), 500))
    return JobRun.query.order_by(JobRun.created_at.desc()).limit(limit).all()


def enqueue_job(*, job_type: str, queue_key: str, payload: Optional[Dict[str, Any]] = None, requested_by_user_id: Optional[int] = None) -> JobRun:
    clean_job_type = (job_type or "").strip()
    clean_queue_key = (queue_key or "").strip()
    if not JOB_TYPE_RE.match(clean_job_type):
        raise ValueError("job_type non valido")

    payload_data = payload or {}
    if not isinstance(payload_data, dict):
        raise ValueError("payload deve essere un object JSON")
    try:
        payload_raw = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"))
    except Exception as ex:
        raise ValueError(f"payload non serializzabile: {ex}") from ex
    if len(payload_raw.encode("utf-8")) > MAX_JOB_PAYLOAD_BYTES:
        raise ValueError(f"payload troppo grande (max {MAX_JOB_PAYLOAD_BYTES} bytes)")

    rt = current_app.extensions.get("job_runtime")
    if rt is not None and clean_job_type not in (rt.handlers or {}):
        raise ValueError(f"job_type non registrato: {clean_job_type}")
    if rt is not None and not clean_queue_key:
        handler_meta = dict((rt.handler_meta or {}).get(clean_job_type) or {})
        clean_queue_key = str(handler_meta.get("queue_key", "default")).strip() or "default"
    if not QUEUE_KEY_RE.match(clean_queue_key):
        raise ValueError("queue_key non valida")

    default_concurrency = _default_queue_concurrency_from_cfg(current_app.config)
    queue = JobQueue.query.filter_by(queue_key=clean_queue_key).first()
    if not queue:
        queue = JobQueue(
            queue_key=clean_queue_key[:40],
            name=f"Queue {clean_queue_key[:40]}",
            enabled=True,
            concurrency=default_concurrency,
            paused=False,
        )
        db.session.add(queue)
        db.session.flush()

    handler_meta = {}
    if rt is not None:
        handler_meta = dict((rt.handler_meta or {}).get(clean_job_type) or {})
    max_retries = _clamp_int(handler_meta.get("max_retries", 0), 0, MAX_JOB_RETRIES)
    timeout_sec = _clamp_float(handler_meta.get("timeout_sec", 0.0), 0.0, MAX_JOB_TIMEOUT_SEC)

    job = JobRun(
        queue_id=queue.id,
        job_type=clean_job_type[:80],
        status="queued",
        payload=payload_data,
        message="Queued",
        progress=0,
        result={"_runtime": {"attempt": 0, "max_retries": max_retries, "timeout_sec": timeout_sec}},
        requested_by_user_id=requested_by_user_id,
    )
    db.session.add(job)
    db.session.commit()
    _log_job_event(
        "enqueued",
        job_id=job.id,
        queue_id=job.queue_id,
        queue_key=clean_queue_key,
        job_type=clean_job_type,
        requested_by_user_id=requested_by_user_id,
        payload_keys=sorted(list(payload_data.keys()))[:20],
        payload_size_bytes=len(payload_raw.encode("utf-8")),
    )
    if _runtime_backend() == REDIS_BACKEND:
        if not _redis_enqueue_job(clean_queue_key, int(job.id)):
            _log_job_event("redis.enqueue_failed", job_id=job.id, queue_key=clean_queue_key)
    _wake_runtime()
    return job


def enqueue_email_job(*, to_email: str, subject: str, text_body: str, html_body: str | None = None, requested_by_user_id: Optional[int] = None) -> JobRun:
    return enqueue_job(
        job_type="send_email_payload",
        queue_key="email",
        payload={
            "to_email": str(to_email or "").strip(),
            "subject": str(subject or "").strip(),
            "text_body": str(text_body or ""),
            "html_body": str(html_body) if html_body else None,
        },
        requested_by_user_id=requested_by_user_id,
    )


def set_queue_pause(queue_id: int, paused: bool) -> Optional[JobQueue]:
    queue = JobQueue.query.get(queue_id)
    if not queue:
        return None
    queue.paused = bool(paused)
    db.session.commit()
    _log_job_event("queue.pause_toggled", queue_id=queue.id, queue_key=queue.queue_key, paused=bool(queue.paused))
    if _runtime_backend() == REDIS_BACKEND and not queue.paused:
        queued_jobs = (
            JobRun.query.filter_by(queue_id=queue.id, status="queued")
            .order_by(JobRun.created_at.asc())
            .limit(2000)
            .all()
        )
        for row in queued_jobs:
            _redis_enqueue_job(str(queue.queue_key), int(row.id))
    _wake_runtime()
    return queue


def request_job_pause(job_id: int, paused: bool) -> Optional[JobRun]:
    job = _get_job(job_id, fresh=True)
    if not job:
        return None
    if paused:
        if job.status in ("queued", "running"):
            job.status = "paused"
            job.message = "Paused by admin"
    else:
        if job.status == "paused":
            if job.started_at is not None and job.finished_at is None:
                job.status = "running"
                job.message = "Resumed"
            else:
                job.status = "queued"
                job.message = "Re-queued"
    job.heartbeat_at = now_utc()
    db.session.commit()
    _log_job_event(
        "pause_toggled",
        job_id=job.id,
        job_type=job.job_type,
        paused=bool(paused),
        status=job.status,
        requested_by_user_id=job.requested_by_user_id,
    )
    if _runtime_backend() == REDIS_BACKEND and job.status == "queued" and job.queue is not None:
        _redis_enqueue_job(str(job.queue.queue_key or "default"), int(job.id))
    _wake_runtime()
    return job


def request_job_stop(job_id: int) -> Optional[JobRun]:
    job = _get_job(job_id, fresh=True)
    if not job:
        return None
    job.stop_requested = True
    if job.status in ("queued", "paused"):
        job.status = "stopped"
        job.finished_at = now_utc()
        job.progress = min(job.progress or 0, 99)
    job.message = "Stop requested"
    job.heartbeat_at = now_utc()
    db.session.commit()
    _log_job_event(
        "stop_requested",
        job_id=job.id,
        job_type=job.job_type,
        status=job.status,
        progress=job.progress,
        requested_by_user_id=job.requested_by_user_id,
    )
    _wake_runtime()
    return job


def cleanup_terminal_jobs(*, scope: str = "recent", recent_limit: int = 200) -> Dict[str, Any]:
    mode = (scope or "recent").strip().lower()
    if mode not in ("recent", "all"):
        mode = "recent"

    limit = max(1, min(int(recent_limit or 200), 2000))
    q_terminal = JobRun.query.filter(JobRun.status.in_(TERMINAL_JOB_STATUSES))

    if mode == "all":
        deleted = q_terminal.delete(synchronize_session=False)
        db.session.commit()
        _log_job_event("cleanup", scope=mode, recent_limit=limit, deleted=int(deleted))
        return {"scope": mode, "recent_limit": limit, "deleted": int(deleted)}

    recent_rows = (
        JobRun.query.with_entities(JobRun.id, JobRun.status)
        .order_by(JobRun.created_at.desc())
        .limit(limit)
        .all()
    )
    candidate_ids = [int(jid) for jid, status in recent_rows if status in TERMINAL_JOB_STATUSES]
    if not candidate_ids:
        return {"scope": mode, "recent_limit": limit, "deleted": 0}

    deleted = JobRun.query.filter(JobRun.id.in_(candidate_ids)).delete(synchronize_session=False)
    db.session.commit()
    _log_job_event("cleanup", scope=mode, recent_limit=limit, deleted=int(deleted))
    return {"scope": mode, "recent_limit": limit, "deleted": int(deleted)}


def serialize_queue(q: JobQueue) -> Dict[str, Any]:
    return {
        "id": q.id,
        "queue_key": q.queue_key,
        "name": q.name,
        "enabled": bool(q.enabled),
        "paused": bool(q.paused),
        "concurrency": int(q.concurrency or 1),
        "updated_at": q.updated_at.isoformat() if q.updated_at else None,
    }


def serialize_job(job: JobRun) -> Dict[str, Any]:
    rt = (job.result or {}).get("_runtime", {}) if isinstance(job.result, dict) else {}
    return {
        "id": job.id,
        "queue_id": job.queue_id,
        "queue_key": job.queue.queue_key if job.queue else None,
        "job_type": job.job_type,
        "status": job.status,
        "progress": int(job.progress or 0),
        "message": job.message,
        "payload": job.payload or {},
        "result": job.result or {},
        "attempt": _clamp_int(rt.get("attempt", 0), 0, MAX_JOB_RETRIES + 1),
        "max_retries": _clamp_int(rt.get("max_retries", 0), 0, MAX_JOB_RETRIES),
        "requested_by_user_id": job.requested_by_user_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "stop_requested": bool(job.stop_requested),
    }


def job_payload_from_text(raw: str) -> Dict[str, Any]:
    txt = (raw or "").strip()
    if not txt:
        return {}
    parsed = json.loads(txt)
    if not isinstance(parsed, dict):
        raise ValueError("Payload deve essere un JSON object")
    return parsed


def runtime_status() -> Dict[str, Any]:
    """Return runtime mode and worker health info for UI/observability."""
    mode = str(current_app.config.get("JOB_RUNTIME_MODE", "hybrid")).strip().lower()
    role = str(current_app.config.get("JOB_PROCESS_ROLE", "web")).strip().lower()
    rt = current_app.extensions.get("job_runtime")
    thread_alive = bool(getattr(rt, "thread", None) and rt.thread and rt.thread.is_alive()) if rt is not None else False
    backend = _runtime_backend()
    redis_connected = False
    if backend == REDIS_BACKEND and isinstance(rt, _Runtime):
        redis_connected = rt._redis_client() is not None
    return {
        "mode": mode,
        "backend": backend,
        "redis_connected": redis_connected,
        "process_role": role,
        "autostart": bool(current_app.config.get("JOB_RUNTIME_AUTOSTART", True)),
        "thread_alive": thread_alive,
        "poll_interval_sec": float(current_app.config.get("JOB_RUNTIME_POLL_SEC", 0.2)),
    }
