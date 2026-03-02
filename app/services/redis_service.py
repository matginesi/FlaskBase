from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

from flask import current_app

from ..models import JobQueue, JobRun


def _mask_redis_url(raw: str) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    try:
        p = urlsplit(txt)
        if p.password:
            user = p.username or ""
            host = p.hostname or ""
            port = f":{p.port}" if p.port else ""
            auth = f"{user}:***@" if user else "***@"
            netloc = f"{auth}{host}{port}"
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        return txt
    return txt


def _try_redis_client(redis_url: str):
    try:
        from redis import Redis  # type: ignore

        return Redis.from_url(redis_url, decode_responses=True, socket_timeout=1.5, socket_connect_timeout=1.5)
    except Exception:
        return None


def redis_runtime_snapshot() -> dict[str, object]:
    """Best-effort Redis diagnostics.

    Redis is optional. If configured (JOB_QUEUE_BACKEND=redis + JOB_QUEUE_REDIS_URL), this snapshot
    will show reachability and queue depth. Otherwise it remains informational.
    """

    backend = str(current_app.config.get("JOB_QUEUE_BACKEND", "db")).strip().lower()
    redis_url = str(current_app.config.get("JOB_QUEUE_REDIS_URL", "")).strip()
    namespace = str(current_app.config.get("JOB_QUEUE_REDIS_NAMESPACE", "flaskbase")).strip() or "flaskbase"
    out: dict[str, object] = {
        "backend": backend,
        "enabled": backend == "redis",
        "url_masked": _mask_redis_url(redis_url),
        "namespace": namespace,
        "connected": False,
        "ping_ms": None,
        "queue_keys": 0,
        "pending_jobs": 0,
        "db_queued_jobs": int(JobRun.query.filter_by(status="queued").count()),
        "error": "",
        "ratelimit_storage_uri": str(current_app.config.get("RATELIMIT_STORAGE_URI", "")).strip(),
    }

    if backend != "redis" or not redis_url:
        return out

    queue_keys = [str(q.queue_key) for q in JobQueue.query.filter_by(enabled=True).all()]
    out["queue_keys"] = len(queue_keys)

    client = _try_redis_client(redis_url)
    if client is None:
        out["error"] = "Redis client not available (missing dependency or invalid URL)."
        return out

    try:
        t0 = time.perf_counter()
        client.ping()
        out["ping_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        pending = 0
        for qk in queue_keys:
            pending += int(client.llen(f"{namespace}:jobs:queue:{qk}") or 0)
        out["pending_jobs"] = pending
        out["connected"] = True
        return out
    except Exception as ex:
        out["error"] = str(ex)[:180]
        return out


def redis_ping() -> tuple[bool, str, float | None]:
    """(ok, message, ping_ms)"""
    redis_url = str(current_app.config.get("JOB_QUEUE_REDIS_URL", "")).strip()
    if not redis_url:
        return False, "Redis URL not configured.", None
    client = _try_redis_client(redis_url)
    if client is None:
        return False, "Redis client not available.", None
    try:
        t0 = time.perf_counter()
        client.ping()
        return True, "Redis ping OK.", round((time.perf_counter() - t0) * 1000.0, 1)
    except Exception as ex:
        return False, str(ex)[:180], None


def redis_flush_namespace(prefix: str) -> tuple[int, str]:
    """Delete keys matching prefix* (best-effort). Returns (deleted_count, message)."""
    redis_url = str(current_app.config.get("JOB_QUEUE_REDIS_URL", "")).strip()
    if not redis_url:
        return 0, "Redis URL not configured."
    client = _try_redis_client(redis_url)
    if client is None:
        return 0, "Redis client not available."

    try:
        pattern = f"{prefix}*"
        keys = list(client.scan_iter(match=pattern, count=500))
        if not keys:
            return 0, f"No keys found for prefix '{prefix}'."
        deleted = int(client.delete(*keys) or 0)
        return deleted, f"Deleted {deleted} keys for prefix '{prefix}'."
    except Exception as ex:
        return 0, str(ex)[:180]
