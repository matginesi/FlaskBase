from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from flask import current_app, g, has_request_context, request


_RUNTIME_KEYS = {
    "APP_NAME",
    "APP_VERSION",
    "BASE_URL",
    "THEME",
    "VISUAL",
    "SETTINGS",
    "SECURITY",
    "LOGGING",
    "EMAIL",
    "AUTH",
    "API",
    "API_ACCESS",
    "ADDONS",
    "ADDON_POLICIES",
    "ADDON_SETTINGS",
    "DASHBOARD",
}


def get_client_ip() -> str | None:
    """Return the client IP as normalized by ProxyFix when configured."""
    return (request.remote_addr or "").strip()[:64] or None


def get_runtime_config() -> dict[str, Any]:
    if has_request_context():
        runtime_cfg = getattr(g, "runtime_config", None)
        if isinstance(runtime_cfg, dict):
            return runtime_cfg
    return {key: current_app.config.get(key) for key in _RUNTIME_KEYS}


def get_runtime_config_value(key: str, default: Any = None) -> Any:
    runtime_cfg = get_runtime_config()
    value = runtime_cfg.get(key)
    return default if value is None else value


def get_runtime_config_dict(key: str) -> dict[str, Any]:
    value = get_runtime_config_value(key, {})
    return dict(value or {}) if isinstance(value, dict) else {}


def validate_action_url(raw: str | None) -> str | None:
    if not raw:
        return None
    url = str(raw).strip()[:500]
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return url
