from __future__ import annotations

from typing import Any, Dict, Optional

from .app_logger import get_logger, log_exception as emit_exception


def log_exception(
    exc: BaseException,
    *,
    event_type: str = "exception",
    ctx: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a structured exception event through the unified application logger."""
    try:
        emit_exception(exc, event_type=event_type, logger=get_logger("error_log"), context=dict(ctx or {}))
    except Exception:
        pass
