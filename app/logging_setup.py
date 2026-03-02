from __future__ import annotations

import logging
import os
import re
import traceback
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from flask import Flask, g, request
from flask_login import current_user
from rich.logging import RichHandler

from .utils import get_client_ip
from .services.log_policy import should_persist_event
from .services.redaction import sanitize_for_logs


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_RESIDUAL_ANSI_RE = re.compile(r"\[(?:\d{1,3};?)+m")


def _clean_ansi(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    out = _ANSI_RE.sub("", value)
    out = _RESIDUAL_ANSI_RE.sub("", out)
    return out


def configure_logging(app: Flask) -> None:
    cfg = app.config.get("LOGGING", {}) or {}
    level_name = str(cfg.get("LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = cfg.get("FILE", os.path.join(app.instance_path, "app.log"))
    max_bytes = int(cfg.get("MAX_BYTES", 10 * 1024 * 1024))
    backup_count = int(cfg.get("BACKUP_COUNT", 5))
    mask_sensitive = str((app.config.get("SECURITY", {}) or {}).get("MASK_SENSITIVE_LOG_DATA", True)).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    try:
        max_context_str = int(cfg.get("MAX_CONTEXT_STRING_LEN", 2000))
    except Exception:
        max_context_str = 2000
    max_context_str = max(120, min(12000, max_context_str))
    db_level_name = str(cfg.get("DB_LEVEL", "INFO")).upper()
    db_level = getattr(logging, db_level_name, logging.INFO)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers in dev reload
    root.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s | %(message)s | ctx=%(context)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    class ContextFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # Remove ANSI color codes coming from upstream loggers (e.g. werkzeug access logs).
            record.msg = _clean_ansi(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_clean_ansi(v) for v in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _clean_ansi(v) for k, v in record.args.items()}

            # Rich context without breaking when no request context exists
            context: Dict[str, Any] = getattr(record, "context", {}) or {}
            try:
                context.setdefault("path", request.path)
                context.setdefault("method", request.method)
                context.setdefault("ip", get_client_ip())
                context.setdefault("ua", request.user_agent.string[:160] if request.user_agent else None)
                context.setdefault("request_id", getattr(g, "request_id", None))
            except Exception:
                pass
            record.context = sanitize_for_logs(
                context,
                mask_enabled=mask_sensitive,
                # Keep textual fields in persisted logs; visibility is controlled at UI level.
                allow_text_content=True,
                max_string_len=max_context_str,
            )
            if record.exc_info:
                try:
                    record.context["traceback"] = "".join(traceback.format_exception(*record.exc_info))[-8000:]
                except Exception:
                    record.context["traceback"] = "<traceback unavailable>"
            return True

    # Console handler
    use_rich = str(os.getenv("CLI_RICH_LOGS", "")).lower() in ("1", "true", "yes", "on")
    if use_rich:
        ch = RichHandler(
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True,
        )
        ch.setLevel(level)
        # RichHandler already formats columns; keep message compact.
        ch.setFormatter(logging.Formatter("%(message)s | ctx=%(context)s"))
        ch.addFilter(ContextFilter())
        root.addHandler(ch)
    else:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(fmt)
        ch.addFilter(ContextFilter())
        root.addHandler(ch)

    # Rotating file handler
    fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    fh.addFilter(ContextFilter())
    root.addHandler(fh)

    # Optional DB persistence handler (best-effort): capture warnings/errors in LogEvent.
    # This makes the "Logs" UI reflect problems coming from anywhere in the codebase,
    # including add-ons that simply use `logging.getLogger(__name__)`.
    try:
        from sqlalchemy.exc import SQLAlchemyError

        from .extensions import db
        from .models import LogEvent

        class DbLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    # Avoid noisy/system loggers and recursion.
                    name = str(getattr(record, "name", ""))
                    if name.startswith("sqlalchemy") or name.startswith("alembic"):
                        return
                    lvl = int(getattr(record, "levelno", logging.INFO))
                    if lvl < db_level:
                        return
                    # Flask request context may not exist; context filter guards for that.
                    ctx = getattr(record, "context", {}) or {}
                    msg = record.getMessage()
                    event_type = str(getattr(record, "event_type", "") or name or "log")[:80]
                    if not should_persist_event(
                        app,
                        channel="db_log",
                        level=getattr(record, "levelname", lvl),
                        event_type=event_type,
                        logger_name=name,
                        message=msg,
                        context=ctx,
                    ):
                        return
                    user_id = None
                    try:
                        if getattr(current_user, "is_authenticated", False):
                            user_id = getattr(current_user, "id", None)
                    except Exception:
                        user_id = None
                    payload = {
                        "level": str(getattr(record, "levelname", "WARNING"))[:16],
                        "event_type": event_type,
                        "message": str(msg)[:500],
                        "user_id": user_id,
                        "ip": str(ctx.get("ip") or "")[:64] or None,
                        "path": str(ctx.get("path") or "")[:200] or None,
                        "method": str(ctx.get("method") or "")[:12] or None,
                        "context": dict(ctx, logger=name),
                    }
                    engine = db.session.get_bind()
                    with engine.begin() as conn:
                        conn.execute(LogEvent.__table__.insert().values(**payload))
                except SQLAlchemyError:
                    # Never raise from logging.
                    pass
                except Exception:
                    pass

        dbh = DbLogHandler()
        dbh.setLevel(db_level)
        dbh.addFilter(ContextFilter())
        root.addHandler(dbh)
    except Exception:
        pass

    app.logger.info("Logging configured", extra={"context": {"level": level_name, "file": log_file}})
