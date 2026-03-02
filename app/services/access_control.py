from __future__ import annotations

from typing import Any

from flask import current_app, has_app_context


def _role_of(user: Any) -> str:
    if user is None:
        return "anonymous"
    return str(getattr(user, "role", "anonymous") or "anonymous").strip().lower()


def _is_authenticated(user: Any) -> bool:
    try:
        return bool(getattr(user, "is_authenticated", False))
    except Exception:
        return False


def _cfg(app: Any = None) -> dict[str, Any]:
    if app is not None:
        return dict(getattr(app, "config", {}) or {})
    if has_app_context():
        return dict(current_app.config or {})
    return {}


def addon_policy(addon_key: str, app: Any = None) -> dict[str, Any]:
    items = _cfg(app).get("ADDON_POLICIES", {}) or {}
    entry = items.get(addon_key, {}) if isinstance(items, dict) else {}
    if not isinstance(entry, dict):
        entry = {}
    return {"enabled": bool(entry.get("enabled", True))}


def addon_enabled(addon_key: str, app: Any = None) -> bool:
    return bool(addon_policy(addon_key, app=app).get("enabled", True))


def can_access_addon(addon_key: str, user: Any, app: Any = None) -> bool:
    return addon_enabled(addon_key, app=app) and _is_authenticated(user) and _role_of(user) in {"user", "admin"}


def can_access_addon_api(addon_key: str, user: Any, app: Any = None) -> bool:
    return can_access_addon(addon_key, user, app=app)


def addon_access_map(user: Any) -> dict[str, bool]:
    items = _cfg().get("ADDON_POLICIES", {}) or {}
    out: dict[str, bool] = {}
    if isinstance(items, dict):
        for key in items.keys():
            out[str(key)] = can_access_addon(str(key), user)
    return out
