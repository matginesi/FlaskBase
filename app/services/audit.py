from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import current_app, g, has_request_context, request, session
from flask_login import current_user

from ..extensions import db
from ..models import LogEvent, now_utc
from ..utils import get_client_ip
from .log_policy import should_persist_event
from .redaction import sanitize_for_logs

log = logging.getLogger("audit")


def _build_context(base: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx: Dict[str, Any] = dict(base or {})
    if has_request_context():
        ctx.setdefault("request_id", getattr(g, "request_id", None))
        ctx.setdefault("endpoint", request.endpoint)
        ctx.setdefault("path", request.path)
        ctx.setdefault("method", request.method)
        ctx.setdefault("ip", get_client_ip())
        ctx.setdefault("query", request.query_string.decode("utf-8", errors="ignore")[:300])
        ctx.setdefault("referer", (request.headers.get("Referer") or "")[:200])
        ua = request.user_agent.string if request.user_agent else None
        ctx.setdefault("user_agent", (ua or "")[:200])
        ctx.setdefault("is_secure", bool(request.is_secure))
        ctx.setdefault("host", (request.host or "")[:120])
        sid = session.get("_id") if isinstance(session, dict) else None
        if sid:
            ctx.setdefault("session_ref", str(sid)[:40])
    if has_request_context() and getattr(current_user, "is_authenticated", False):
        ctx.setdefault("user_id", getattr(current_user, "id", None))
        ctx.setdefault("user_email", getattr(current_user, "email", None))
        ctx.setdefault("user_role", getattr(current_user, "role", None))
    elif has_request_context() and getattr(g, "api_user", None) is not None:
        api_user = getattr(g, "api_user")
        ctx.setdefault("user_id", getattr(api_user, "id", None))
        ctx.setdefault("user_email", getattr(api_user, "email", None))
        ctx.setdefault("user_role", getattr(api_user, "role", None))
    ctx.setdefault("ts_iso", now_utc().isoformat(timespec="seconds") + "Z")
    return ctx


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: _json_safe(v) for k, v in list(value.items())[:80]}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in list(value)[:80]]
    return str(value)[:300]


def audit(event_type: str, message: str, *, level: str = "INFO", context: Optional[Dict[str, Any]] = None) -> None:
    """Persist an audit event in DB (and you can still log to file separately)."""
    sec = dict(current_app.config.get("SECURITY", {}) or {}) if has_request_context() else {}
    mask_sensitive = str(sec.get("MASK_SENSITIVE_LOG_DATA", True)).strip().lower() in ("1", "true", "yes", "on")
    merged_ctx = sanitize_for_logs(
        _json_safe(_build_context(context)),
        mask_enabled=mask_sensitive,
        # Textual fields are always persisted; UI controls visibility in log pages.
        allow_text_content=True,
        max_string_len=2500,
    )
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    try:
        if should_persist_event(
            current_app,
            channel="audit",
            level=level,
            event_type=event_type,
            logger_name="audit",
            message=message,
            context=merged_ctx,
        ):
            payload = {
                "level": str(level).upper()[:16],
                "event_type": str(event_type or "audit")[:80],
                "message": str(message or "")[:500],
                "user_id": (
                    getattr(current_user, "id", None)
                    if has_request_context() and getattr(current_user, "is_authenticated", False)
                    else getattr(getattr(g, "api_user", None), "id", None) if has_request_context() else None
                ),
                "ip": merged_ctx.get("ip"),
                "path": merged_ctx.get("path"),
                "method": merged_ctx.get("method"),
                "context": merged_ctx,
            }
            engine = db.session.get_bind()
            with engine.begin() as conn:
                conn.execute(LogEvent.__table__.insert().values(**payload))

        # Mirror audit events to file/console logger with structured context.
        log.log(lvl, "%s | %s", event_type, message[:500], extra={"context": merged_ctx})
    except Exception:
        # Never break request flow due to audit logging.
        try:
            db.session.rollback()
        except Exception:
            pass
