from __future__ import annotations
from datetime import date, datetime, time as dtime, timedelta
import os
import math
import logging
import resource
import time
from urllib.parse import urlsplit, urlunsplit
from flask import Blueprint, render_template, redirect, url_for, current_app, request, jsonify, session
from flask_login import current_user, login_required
from ...extensions import db
from ...models import BroadcastMessage, BroadcastMessageRead, JobQueue, JobRun, LogEvent, User, UserMessage, now_utc
from ...services.audit import audit
from ...services.job_service import runtime_status as job_runtime_status
from ...services.message_delivery_service import unread_message_counts_for_user
from ...services.redis_service import redis_runtime_snapshot
from ...services.runtime_control import read_runtime_control
from ...utils import get_runtime_config_dict
def render_placeholders(text: str, details: dict | None = None) -> str:
    return text

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)


_LAST_CPU_SAMPLE: tuple[float, int] | None = None  # (total_jiffies, idle_jiffies)


def _cpu_usage_pct() -> float | None:
    """Best-effort CPU percent using /proc/stat deltas (Linux)."""
    global _LAST_CPU_SAMPLE
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            line = (fh.readline() or "").strip()
        if not line.startswith("cpu "):
            return None
        parts = [p for p in line.split() if p]
        nums = [int(x) for x in parts[1:9]]  # user, nice, system, idle, iowait, irq, softirq, steal
        if len(nums) < 4:
            return None
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        if _LAST_CPU_SAMPLE is None:
            # First sample: take a quick second sample to avoid showing "—" right after startup
            _LAST_CPU_SAMPLE = (float(total), int(idle))
            try:
                time.sleep(0.12)
                with open("/proc/stat", "r", encoding="utf-8") as fh2:
                    line2 = (fh2.readline() or "").strip()
                if not line2.startswith("cpu "):
                    return None
                parts2 = [p for p in line2.split() if p]
                nums2 = [int(x) for x in parts2[1:9]]
                if len(nums2) < 4:
                    return None
                idle2 = nums2[3] + (nums2[4] if len(nums2) > 4 else 0)
                total2 = sum(nums2)
                prev_total, prev_idle = _LAST_CPU_SAMPLE
                dt_total = float(total2) - float(prev_total)
                dt_idle = float(idle2) - float(prev_idle)
                _LAST_CPU_SAMPLE = (float(total2), int(idle2))
                if dt_total <= 0:
                    return None
                used = max(0.0, min(1.0, (dt_total - dt_idle) / dt_total))
                return round(used * 100.0, 1)
            except Exception:
                return None
        prev_total, prev_idle = _LAST_CPU_SAMPLE
        dt_total = float(total) - float(prev_total)
        dt_idle = float(idle) - float(prev_idle)
        _LAST_CPU_SAMPLE = (float(total), int(idle))
        if dt_total <= 0:
            return None
        used = max(0.0, min(1.0, (dt_total - dt_idle) / dt_total))
        return round(used * 100.0, 1)
    except Exception:
        return None


def _mem_snapshot() -> dict[str, float] | None:
    """Return total/used/free/pct (MB) from /proc/meminfo (Linux)."""
    try:
        kv: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                num = (v.strip().split() or ["0"])[0]
                try:
                    kv[k.strip()] = int(num)
                except Exception:
                    continue
        total_kb = kv.get("MemTotal")
        avail_kb = kv.get("MemAvailable")
        if not total_kb or not avail_kb:
            return None
        used_kb = max(0, int(total_kb) - int(avail_kb))
        total_mb = round(total_kb / 1024.0, 1)
        used_mb = round(used_kb / 1024.0, 1)
        free_mb = round(avail_kb / 1024.0, 1)
        pct = round((used_kb / total_kb) * 100.0, 1) if total_kb > 0 else 0.0
        return {"total_mb": total_mb, "used_mb": used_mb, "free_mb": free_mb, "used_pct": pct}
    except Exception:
        return None


def _db_snapshot() -> dict[str, object]:
    """Cross-DB snapshot: dialect + best-effort size."""
    from sqlalchemy import text
    uri = str(current_app.config.get("SQLALCHEMY_DATABASE_URI", "") or "")
    out: dict[str, object] = {"dialect": "", "size_mb": None, "error": ""}
    if not uri:
        return out
    out["dialect"] = uri.split(":", 1)[0]
    try:
        if uri.startswith("sqlite"):
            # sqlite: file path after ///
            p = uri.split("///", 1)[-1]
            if p and os.path.exists(p):
                out["size_mb"] = round(os.path.getsize(p) / (1024.0 * 1024.0), 2)
        elif uri.startswith("postgres"):
            # Best-effort Postgres DB size (may require permissions).
            sz = db.session.execute(text("SELECT pg_database_size(current_database())"))
            val = sz.scalar()
            if val is not None:
                out["size_mb"] = round(float(val) / (1024.0 * 1024.0), 2)
        else:
            # other engines: keep optional
            out["size_mb"] = None
    except Exception as ex:
        out["error"] = str(ex)[:160]
    return out


def _process_rss_mb() -> float | None:
    """Best-effort current process RSS in MB (Linux first, portable fallback)."""
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            parts = (fh.read() or "").strip().split()
        if len(parts) >= 2:
            rss_pages = int(parts[1])
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round((rss_pages * page_size) / (1024 * 1024), 1)
    except Exception:
        pass
    try:
        rss_kb = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0.0)
        if rss_kb > 0:
            return round(rss_kb / 1024.0, 1)
    except Exception:
        pass
    return None


def _memory_snapshot() -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "total_mb": None,
        "available_mb": None,
        "used_mb": None,
        "used_pct": None,
        "available_pct": None,
        "cached_pct": None,
    }
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                k, rest = line.split(":", 1)
                val = (rest.strip().split() or ["0"])[0]
                mem[k.strip()] = int(val)

        total_kb = int(mem.get("MemTotal", 0))
        avail_kb = int(mem.get("MemAvailable", 0))
        if total_kb <= 0:
            return out
        if avail_kb <= 0:
            avail_kb = int(mem.get("MemFree", 0)) + int(mem.get("Cached", 0)) + int(mem.get("Buffers", 0))
        used_kb = max(0, total_kb - avail_kb)
        cached_kb = int(mem.get("Cached", 0))

        out["total_mb"] = round(total_kb / 1024.0, 1)
        out["available_mb"] = round(avail_kb / 1024.0, 1)
        out["used_mb"] = round(used_kb / 1024.0, 1)
        out["used_pct"] = round((used_kb / total_kb) * 100.0, 1)
        out["available_pct"] = round((avail_kb / total_kb) * 100.0, 1)
        out["cached_pct"] = round((cached_kb / total_kb) * 100.0, 1)
        return out
    except Exception:
        return out


def _disk_snapshot(path: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "total_gb": None,
        "used_gb": None,
        "free_gb": None,
        "used_pct": None,
    }
    try:
        stat = os.statvfs(path)
        total = int(stat.f_blocks) * int(stat.f_frsize)
        free = int(stat.f_bavail) * int(stat.f_frsize)
        used = max(0, total - free)
        if total <= 0:
            return out
        out["total_gb"] = round(total / (1024.0 ** 3), 1)
        out["used_gb"] = round(used / (1024.0 ** 3), 1)
        out["free_gb"] = round(free / (1024.0 ** 3), 1)
        out["used_pct"] = round((used / total) * 100.0, 1)
        return out
    except Exception:
        return out




def _uptime_seconds() -> int | None:
    """Best-effort system uptime in seconds.

    Linux: reads /proc/uptime. Fallback returns None.
    """
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            raw = f.read().strip().split()
        if raw:
            return int(float(raw[0]))
    except Exception:
        return None
    return None


def _gunicorn_snapshot() -> dict[str, object]:
    server_software = " ".join(
        [
            str(request.environ.get("SERVER_SOFTWARE", "") or "").strip(),
            str(os.getenv("SERVER_SOFTWARE", "") or "").strip(),
        ]
    ).lower()
    def _env_int(name: str, default: int) -> int:
        try:
            return max(1, int(str(os.getenv(name, default)).strip()))
        except Exception:
            return default

    workers = _env_int("GUNICORN_WORKERS", 1)
    threads = _env_int("GUNICORN_THREADS", 1)
    timeout = _env_int("GUNICORN_TIMEOUT", 120)
    reload_enabled = str(os.getenv("GUNICORN_RELOAD", "false")).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "detected": "gunicorn" in server_software,
        "server_software": server_software or "",
        "workers": workers,
        "threads": threads,
        "timeout": timeout,
        "reload": reload_enabled,
        "capacity": workers * threads,
    }


def _jobs_snapshot() -> dict[str, object]:
    runtime = job_runtime_status()
    queued = JobRun.query.filter_by(status="queued").count()
    running = JobRun.query.filter_by(status="running").count()
    completed_24h = JobRun.query.filter(
        JobRun.status == "completed",
        JobRun.finished_at.isnot(None),
        JobRun.finished_at >= now_utc() - timedelta(hours=24),
    ).count()
    failed_24h = JobRun.query.filter(
        JobRun.status.in_(("failed", "stopped")),
        JobRun.finished_at.isnot(None),
        JobRun.finished_at >= now_utc() - timedelta(hours=24),
    ).count()
    queues_enabled = JobQueue.query.filter_by(enabled=True).count()
    queues_paused = JobQueue.query.filter_by(paused=True).count()
    recent = (
        JobRun.query.order_by(JobRun.created_at.desc())
        .limit(8)
        .all()
    )
    return {
        "queued": queued,
        "running": running,
        "completed_24h": completed_24h,
        "failed_24h": failed_24h,
        "queues_enabled": queues_enabled,
        "queues_paused": queues_paused,
        "runtime": runtime,
        "recent": [
            {
                "id": int(job.id),
                "job_type": str(job.job_type or ""),
                "status": str(job.status or ""),
                "queue_key": str(job.queue.queue_key if job.queue else ""),
                "progress": int(job.progress or 0),
            }
            for job in recent
        ],
    }


def _dashboard_stats():
    # DB-agnostic "today" filter: works on SQLite + Postgres
    today_d = now_utc().date()
    start = datetime.combine(today_d, dtime.min)
    end = start + timedelta(days=1)

    log_today = LogEvent.query.filter(LogEvent.ts >= start, LogEvent.ts < end).count()
    logins_today = LogEvent.query.filter(
        LogEvent.ts >= start,
        LogEvent.ts < end,
        LogEvent.event_type == "auth.login_success",
    ).count()
    messages_total = BroadcastMessage.query.count()

    return {
        "total_users": User.query.count(),
        "active_users": User.query.filter_by(is_active=True).count(),
        "messages_total": messages_total,
        "log_today": log_today,
        "logins_today": logins_today,
        "warnings": LogEvent.query.filter_by(level="WARNING").count(),
        "errors": LogEvent.query.filter_by(level="ERROR").count(),
        "cpu_pct": _cpu_usage_pct(),
        "load_1": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
        "load_5": round(os.getloadavg()[1], 2) if hasattr(os, "getloadavg") else None,
        "load_15": round(os.getloadavg()[2], 2) if hasattr(os, "getloadavg") else None,
        "cpu_cores": int(os.cpu_count() or 0) or None,
        "uptime_s": _uptime_seconds(),
        "ram": _mem_snapshot(),
        "disk": _disk_snapshot(str(current_app.config.get("DISK_PATH") or current_app.instance_path or "/")),
        "db": _db_snapshot(),
        "redis": redis_runtime_snapshot(),
        "gunicorn": _gunicorn_snapshot(),
        "jobs": _jobs_snapshot(),
    }


@bp.post("/set-language", endpoint="set_language_post")
@login_required
def set_language_post():
    lang = str(request.form.get("lang") or "").strip().lower()
    if lang not in ("en", "it"):
        lang = "en"
    session["ui_lang"] = lang
    try:
        current_user.locale = "it-IT" if lang == "it" else "en"
        db.session.commit()
    except Exception:
        db.session.rollback()
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.get("/")
def index():
    return redirect(url_for("main.dashboard"))


@bp.get("/dashboard")
@login_required
def dashboard():
    stats = _dashboard_stats()
    visual = get_runtime_config_dict("VISUAL")
    dash_visual = visual.get("DASHBOARD") if isinstance(visual.get("DASHBOARD"), dict) else {}
    try:
        recent_max = int(dash_visual.get("recent_events_max", 10))
    except Exception:
        recent_max = 10
    recent_max = max(5, min(50, recent_max))
    recent_events = LogEvent.query.order_by(LogEvent.ts.desc()).limit(recent_max).all()

    dash_cfg = dict(current_app.config.get("DASHBOARD", {}) or {})
    dashboard_auto_refresh_enabled = bool(dash_cfg.get("auto_refresh_enabled", True))
    try:
        dashboard_auto_refresh_sec = int(dash_cfg.get("auto_refresh_sec", 8))
    except Exception:
        dashboard_auto_refresh_sec = 8
    dashboard_auto_refresh_sec = max(5, min(10, dashboard_auto_refresh_sec))

    user_broadcasts = []
    user_broadcasts_enabled = current_user.role != "admin"
    if user_broadcasts_enabled:
        rows = BroadcastMessage.query.order_by(BroadcastMessage.created_at.desc()).limit(8).all()
        user_broadcasts = [m for m in rows if m.is_visible()]

    # Messages inbox preview (unread badge + last N)
    inbox_preview = {"unread_count": 0, "recent": []}
    try:
        # Broadcast unread
        b_rows = BroadcastMessage.query.order_by(BroadcastMessage.created_at.desc()).limit(12).all()
        b_visible = [m for m in b_rows if m.is_visible()]
        b_ids = [m.id for m in b_visible]
        b_read = set()
        if b_ids:
            b_read = set(
                r[0]
                for r in db.session.query(BroadcastMessageRead.message_id)
                .filter(BroadcastMessageRead.user_id == current_user.id, BroadcastMessageRead.message_id.in_(b_ids))
                .all()
            )
        b_unread = [m for m in b_visible if m.id not in b_read]

        # User unread
        u_rows = (
            UserMessage.query.filter_by(user_id=current_user.id, is_read=False)
            .order_by(UserMessage.created_at.desc())
            .limit(12)
            .all()
        )
        u_visible = [m for m in u_rows if m.is_visible()]

        recent = []
        for m in (u_visible[:6] + b_unread[:6]):
            recent.append(
                {
                    "kind": "user" if hasattr(m, "user_id") else "broadcast",
                    "title": m.title,
                    "level": m.level or "info",
                    "created_at": m.created_at,
                }
            )
        recent.sort(key=lambda x: (x.get("created_at") or datetime.min), reverse=True)
        inbox_preview["unread_count"] = len(b_unread) + len(u_visible)
        inbox_preview["recent"] = recent[:5]
    except Exception:
        inbox_preview = {"unread_count": 0, "recent": []}

    admin_panel = None
    if current_user.role == "admin":
        from sqlalchemy import func

        counts_raw = db.session.query(LogEvent.level, func.count()).group_by(LogEvent.level).all()
        level_counts = {lv: cnt for lv, cnt in counts_raw}

        connected_window_min = 10
        cutoff = now_utc() - timedelta(minutes=connected_window_min)
        connected_q = (
            db.session.query(
                User.id.label("uid"),
                User.name.label("name"),
                User.email.label("email"),
                User.role.label("role"),
                func.max(LogEvent.ts).label("last_seen"),
            )
            .join(LogEvent, LogEvent.user_id == User.id)
            .filter(LogEvent.user_id.isnot(None), LogEvent.ts >= cutoff)
            .group_by(User.id, User.name, User.email, User.role)
            .order_by(func.max(LogEvent.ts).desc())
        )
        connected_users = connected_q.limit(12).all()

        # 14-day cumulative users series for sparkline.
        horizon_days = 14
        today_d = now_utc().date()
        start_d = today_d - timedelta(days=horizon_days - 1)
        created_dates = [
            created_at.date()
            for (created_at,) in User.query.with_entities(User.created_at).all()
            if created_at is not None
        ]
        created_dates.sort()
        users_series = []
        cursor = 0
        running = 0
        for offset in range(horizon_days):
            day = start_d + timedelta(days=offset)
            while cursor < len(created_dates) and created_dates[cursor] <= day:
                running += 1
                cursor += 1
            users_series.append(running)

        admin_panel = {
            "latest_users": User.query.order_by(User.created_at.desc()).limit(6).all(),
            "level_counts": level_counts,
            "connected_users": connected_users,
            "connected_count": len(connected_users),
            "connected_window_min": connected_window_min,
            "users_series": users_series,
        }

    audit("page.view", "Viewed dashboard")
    return render_template(
        "main/dashboard.html",
        stats=stats,
        recent_events=recent_events,
        admin_panel=admin_panel,
        user_broadcasts=user_broadcasts,
        inbox=inbox_preview,
        user_broadcasts_enabled=user_broadcasts_enabled,
        dashboard_auto_refresh_enabled=dashboard_auto_refresh_enabled,
        dashboard_auto_refresh_sec=dashboard_auto_refresh_sec,
        visual=get_runtime_config_dict("VISUAL"),
    )

@bp.get("/metrics")
@login_required
def metrics():
    """Lightweight metrics endpoint used by the dashboard (AJAX polling).
    Returns only non-sensitive metrics for non-admin users.
    """
    if getattr(current_user, "role", "") != "admin":
        # For regular users: only message counters (safe)
        counts = unread_message_counts_for_user(getattr(current_user, "id", None))
        return jsonify({"ts": int(time.time()), **counts})

    stats = _dashboard_stats()
    ram = stats.get("ram") or {}
    disk = stats.get("disk") or {}
    gunicorn = stats.get("gunicorn") or {}
    jobs = stats.get("jobs") or {}
    runtime = jobs.get("runtime") if isinstance(jobs, dict) else {}
    return jsonify({
        "ts": int(time.time()),
        "cpu_pct": stats.get("cpu_pct"),
        "load_1": stats.get("load_1"),
        "load_5": stats.get("load_5"),
        "load_15": stats.get("load_15"),
        "cpu_cores": stats.get("cpu_cores"),
        "uptime_s": stats.get("uptime_s"),
        "ram_used_pct": ram.get("used_pct"),
        "ram_used_mb": ram.get("used_mb"),
        "ram_total_mb": ram.get("total_mb"),
        "disk_used_pct": disk.get("used_pct"),
        "disk_used_gb": disk.get("used_gb"),
        "disk_total_gb": disk.get("total_gb"),
        "db": stats.get("db") or {},
        "gunicorn_workers": gunicorn.get("workers"),
        "gunicorn_threads": gunicorn.get("threads"),
        "gunicorn_capacity": gunicorn.get("capacity"),
        "gunicorn_timeout": gunicorn.get("timeout"),
        "job_queued": jobs.get("queued"),
        "job_running": jobs.get("running"),
        "job_completed_24h": jobs.get("completed_24h"),
        "job_failed_24h": jobs.get("failed_24h"),
        "job_queues_enabled": jobs.get("queues_enabled"),
        "job_queues_paused": jobs.get("queues_paused"),
        "job_runtime_thread_alive": runtime.get("thread_alive") if isinstance(runtime, dict) else None,
        "job_runtime_mode": runtime.get("mode") if isinstance(runtime, dict) else None,
        "job_runtime_backend": runtime.get("backend") if isinstance(runtime, dict) else None,
    })


@bp.get("/messages")
@login_required
def messages():
    """User-facing messages (broadcast + per-user) with read/unread state."""
    audit("page.view", "Viewed messages")
    flt = (request.args.get("filter") or "unread").strip().lower()  # unread|all
    kind = (request.args.get("kind") or "all").strip().lower()  # all|broadcast|user

    # Broadcast messages (global)
    b_rows = BroadcastMessage.query.order_by(BroadcastMessage.created_at.desc()).limit(80).all()
    b_visible = [m for m in b_rows if m.is_visible()]
    b_ids = [m.id for m in b_visible]
    read_ids = set()
    if b_ids:
        read_ids = set(
            r[0]
            for r in db.session.query(BroadcastMessageRead.message_id)
            .filter(BroadcastMessageRead.user_id == current_user.id, BroadcastMessageRead.message_id.in_(b_ids))
            .all()
        )

    # User messages (targeted)
    u_rows = (
        UserMessage.query.filter_by(user_id=current_user.id)
        .order_by(UserMessage.created_at.desc())
        .limit(80)
        .all()
    )
    u_visible = [m for m in u_rows if m.is_visible()]

    items: list[dict] = []
    if kind in ("all", "broadcast"):
        for m in b_visible:
            items.append(
                {
                    "kind": "broadcast",
                    "id": m.id,
                    "title": m.title,
                    "body": m.body,
                    "body_format": m.body_format or ("html" if "<" in (m.body or "") and ">" in (m.body or "") else "text"),
                    "level": m.level or "info",
                    "created_at": m.created_at,
                    "is_read": m.id in read_ids,
                    "action_label": m.action_label,
                    "action_url": m.action_url,
                }
            )
    if kind in ("all", "user"):
        for m in u_visible:
            items.append(
                {
                    "kind": "user",
                    "id": m.id,
                    "title": m.title,
                    "body": m.body,
                    "body_format": m.body_format or "text",
                    "level": m.level or "info",
                    "created_at": m.created_at,
                    "is_read": bool(m.is_read),
                    "email_sent_at": m.email_sent_at,
                    "email_error": m.email_error,
                    "action_label": m.action_label,
                    "action_url": m.action_url,
                }
            )

    # Sort combined timeline
    items.sort(key=lambda x: (x.get("created_at") or datetime.min), reverse=True)

    if flt == "unread":
        items = [it for it in items if not it.get("is_read")]
    unread_count = sum(1 for it in items if not it.get("is_read"))

    return render_template(
        "main/messages.html",
        items=items,
        filter=flt,
        kind=kind,
        unread_count=unread_count,
    )


@bp.post("/messages/broadcast/<int:message_id>/read")
@login_required
def mark_broadcast_read(message_id: int):
    try:
        existing = BroadcastMessageRead.query.filter_by(message_id=message_id, user_id=current_user.id).first()
        if not existing:
            db.session.add(BroadcastMessageRead(message_id=message_id, user_id=current_user.id))
            db.session.commit()
            audit("message.read", "Marked broadcast read", context={"message_id": message_id})
    except Exception:
        db.session.rollback()
    return redirect(url_for("main.messages", filter=request.args.get("filter"), kind=request.args.get("kind")))


@bp.post("/messages/user/<int:message_id>/read")
@login_required
def mark_user_message_read(message_id: int):
    try:
        msg = UserMessage.query.filter_by(id=message_id, user_id=current_user.id).first()
        if msg and not msg.is_read:
            msg.is_read = True
            msg.read_at = now_utc()
            db.session.commit()
            audit("message.read", "Marked user message read", context={"message_id": message_id})
    except Exception:
        db.session.rollback()
    return redirect(url_for("main.messages", filter=request.args.get("filter"), kind=request.args.get("kind")))


@bp.get("/privacy")
def privacy():
    if current_user.is_authenticated:
        return render_template("main/privacy.html", now=now_utc())
    return render_template("main/privacy_public.html", now=now_utc())


@bp.get("/runtime/client-state")
@login_required
def runtime_client_state():
    state = read_runtime_control(current_app)
    return jsonify(
        {
            "refresh_token": str(state.get("refresh_token", "")).strip(),
            "refresh_message": str(state.get("refresh_message", "")).strip(),
            "refresh_reason": str(state.get("refresh_reason", "")).strip(),
            "refresh_requested_at": str(state.get("refresh_requested_at", "")).strip(),
        }
    )
@bp.get("/language/<string:lang>")
def set_language(lang: str):
    clean = str(lang or "").strip().lower()
    if clean not in {"en", "it"}:
        clean = "en"
    session["ui_lang"] = clean
    target = request.args.get("next", "") or request.referrer or url_for("main.dashboard")
    return redirect(target)
