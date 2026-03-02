from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "csrf",
    "bearer",
)
_TEXTUAL_KEY_PARTS = (
    "req_text",
    "res_text",
    "reply_text",
    "thinking_text",
    "prompt_text",
    "message_text",
)

_RE_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{8,}")
_RE_SFK = re.compile(r"\bsfk_[A-Za-z0-9_\-]{12,}\b")
_RE_GENERIC_SECRET = re.compile(r"\b(?:sk|pk|rk|tok)_[A-Za-z0-9_\-]{16,}\b", re.IGNORECASE)


def _mask_string(value: str) -> str:
    txt = str(value or "")
    txt = _RE_BEARER.sub("Bearer ***", txt)
    txt = _RE_SFK.sub("sfk_***", txt)
    txt = _RE_GENERIC_SECRET.sub("***", txt)
    return txt


def _is_sensitive_key(key: str) -> bool:
    lk = str(key or "").strip().lower()
    return any(part in lk for part in _SENSITIVE_KEY_PARTS)


def _is_textual_key(key: str) -> bool:
    lk = str(key or "").strip().lower()
    return any(part in lk for part in _TEXTUAL_KEY_PARTS)


def sanitize_for_logs(
    value: Any,
    *,
    mask_enabled: bool = True,
    allow_text_content: bool = True,
    max_string_len: int = 2000,
    _depth: int = 0,
) -> Any:
    if _depth >= 6:
        return "[depth-limited]"

    if value is None or isinstance(value, (int, float, bool)):
        return value

    if isinstance(value, str):
        out = _mask_string(value) if mask_enabled else value
        if len(out) > max_string_len:
            return out[: max(1, max_string_len - 3)] + "..."
        return out

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in list(value.items())[:120]:
            key = str(k)[:120]
            if mask_enabled and _is_sensitive_key(key):
                out[key] = "***"
                continue
            if not allow_text_content and _is_textual_key(key):
                out[key] = "[redacted:text-disabled]"
                continue
            out[key] = sanitize_for_logs(
                v,
                mask_enabled=mask_enabled,
                allow_text_content=allow_text_content,
                max_string_len=max_string_len,
                _depth=_depth + 1,
            )
        return out

    if isinstance(value, (list, tuple, set)):
        return [
            sanitize_for_logs(
                v,
                mask_enabled=mask_enabled,
                allow_text_content=allow_text_content,
                max_string_len=max_string_len,
                _depth=_depth + 1,
            )
            for v in list(value)[:120]
        ]

    return sanitize_for_logs(
        str(value),
        mask_enabled=mask_enabled,
        allow_text_content=allow_text_content,
        max_string_len=max_string_len,
        _depth=_depth + 1,
    )

