from __future__ import annotations

import threading
import time
from typing import Any

from flask import Flask


_STATE_LOCK = threading.Lock()


def _parse_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _level_no(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    raw = str(level or "INFO").strip().upper()
    mapping = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    return mapping.get(raw, 20)


def _starts_with_any(value: str, prefixes: list[str]) -> bool:
    raw = str(value or "")
    return any(raw.startswith(prefix) for prefix in prefixes if prefix)


def _channel_cfg(app: Flask, channel: str) -> dict[str, Any]:
    cfg = dict(app.config.get("LOGGING", {}) or {})
    if channel == "audit":
        return {
            "enabled": str(cfg.get("AUDIT_DB_ENABLED", True)).strip().lower() in {"1", "true", "yes", "on"},
            "min_level": _level_no(cfg.get("AUDIT_DB_LEVEL", "INFO")),
            "skip_event_prefixes": _parse_csv(cfg.get("AUDIT_DB_SKIP_EVENT_PREFIXES", "page.view,message.read")),
            "skip_logger_prefixes": [],
            "window_sec": max(5, min(int(cfg.get("AUDIT_DB_BURST_WINDOW_SEC", 60)), 3600)),
            "burst_max": max(1, min(int(cfg.get("AUDIT_DB_BURST_MAX_PER_KEY", 3)), 100)),
        }
    return {
        "enabled": str(cfg.get("DB_ENABLED", True)).strip().lower() in {"1", "true", "yes", "on"},
        "min_level": _level_no(cfg.get("DB_LEVEL", "INFO")),
        "skip_event_prefixes": _parse_csv(cfg.get("DB_SKIP_EVENT_PREFIXES", "request.completed")),
        "skip_logger_prefixes": _parse_csv(cfg.get("DB_SKIP_LOGGER_PREFIXES", "sqlalchemy,alembic,werkzeug")),
        "window_sec": max(5, min(int(cfg.get("DB_BURST_WINDOW_SEC", 60)), 3600)),
        "burst_max": max(1, min(int(cfg.get("DB_BURST_MAX_PER_KEY", 6)), 100)),
    }


def should_persist_event(
    app: Flask,
    *,
    channel: str,
    level: str | int | None,
    event_type: str | None,
    logger_name: str | None = None,
    message: str | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    cfg = _channel_cfg(app, channel)
    if not cfg["enabled"]:
        return False
    if _level_no(level) < int(cfg["min_level"]):
        return False

    clean_event_type = str(event_type or "").strip()
    clean_logger = str(logger_name or "").strip()
    if _starts_with_any(clean_event_type, list(cfg["skip_event_prefixes"])):
        return False
    if _starts_with_any(clean_logger, list(cfg["skip_logger_prefixes"])):
        return False

    ctx = dict(context or {})
    path = str(ctx.get("path") or "")[:120]
    method = str(ctx.get("method") or "")[:12]
    status = str(ctx.get("status") or "")[:12]
    compact_message = str(message or "")[:120]
    key = "|".join([channel, clean_event_type[:80], clean_logger[:80], path, method, status, compact_message])
    now = time.monotonic()

    with _STATE_LOCK:
        state = app.extensions.setdefault("_log_policy_state", {})
        bucket = state.get(key)
        if not isinstance(bucket, dict) or (now - float(bucket.get("started_at", 0.0))) > float(cfg["window_sec"]):
            state[key] = {"started_at": now, "count": 1}
            if len(state) > 4096:
                cutoff = now - max(30.0, float(cfg["window_sec"]) * 2.0)
                stale = [item_key for item_key, item_val in state.items() if float((item_val or {}).get("started_at", 0.0)) < cutoff]
                for item_key in stale[:2048]:
                    state.pop(item_key, None)
            return True
        bucket["count"] = int(bucket.get("count", 0)) + 1
        return int(bucket["count"]) <= int(cfg["burst_max"])
