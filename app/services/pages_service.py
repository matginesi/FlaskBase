"""Service for DB-backed page and feature configuration."""
from __future__ import annotations

from typing import Any, Dict

from flask import current_app, has_app_context

from ..extensions import db
from ..models import AppSettings


def _read_pages_from_seed() -> Dict[str, Any] | None:
    from .config_service import read_config_json

    try:
        data = read_config_json()
        pages = data.get("PAGES")
        if isinstance(pages, dict):
            return pages
    except Exception:
        pass
    return None


def _normalize_pages_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(data or {})
    pages = out.setdefault("pages", {})
    pages.setdefault("user", {})
    pages.setdefault("admin", {})

    # Modern naming
    settings = out.setdefault("settings", {})
    settings.setdefault("core_pages", pages)

    addons = out.setdefault("addons", {})
    # Backward compatibility: derive addon toggles from user/admin page keys if missing
    for key in ("chat", "api_playground", "widgets", "free_apis", "rag", "docs_viewer", "broadcasts"):
        if key not in addons:
            src = pages.get("user", {}).get(key) or pages.get("admin", {}).get(f"admin_{key}") or {}
            addons[key] = {
                "enabled": bool(src.get("enabled", True)),
                "label": str(src.get("label", key)).replace("_", " ").title(),
                "icon": src.get("icon", "puzzle"),
                "url": src.get("url", f"/{key.replace('_', '-')}"),
                "description": src.get("description", ""),
                "role": "user",
            }

    return out


def read_pages() -> Dict[str, Any]:
    # 1) If runtime settings already cached in app.config, prefer those (DB-backed).
    if has_app_context():
        try:
            cfg = dict(current_app.config.get("APP_CONFIG_EFFECTIVE", {}) or {})
            pages = cfg.get("PAGES")
            if isinstance(pages, dict):
                return _normalize_pages_schema(pages)
        except Exception:
            pass

    # 2) DB-backed runtime row
    if has_app_context():
        try:
            row = db.session.get(AppSettings, 1)
            if row and isinstance(row.pages_json, dict):
                return _normalize_pages_schema(dict(row.pages_json or {}))
        except Exception:
            pass

    # 3) Seed-only defaults from app_config.json
    pages_from_seed = _read_pages_from_seed()
    if isinstance(pages_from_seed, dict):
        return _normalize_pages_schema(pages_from_seed)

    return _normalize_pages_schema({"pages": {"user": {}, "admin": {}}, "addons": {}, "features": {}, "services": {}})


def is_page_enabled(page_key: str, role: str = "user") -> bool:
    """Check if a page is enabled for a given role group."""
    data = read_pages()
    pages = data.get("pages", {})
    group = pages.get(role, {})
    entry = group.get(page_key, {})
    if entry:
        return bool(entry.get("enabled", True))
    addon_entry = data.get("addons", {}).get(page_key, {})
    if addon_entry:
        return bool(addon_entry.get("enabled", True))
    return True


def get_runtime_feature_flags() -> Dict[str, bool]:
    data = read_pages()
    features = dict(data.get("features") or data.get("services") or {})
    return {
        "remember_me": bool(dict(features.get("remember_me") or {}).get("enabled", True)),
        "cookie_banner": bool(dict(features.get("cookie_banner") or {}).get("enabled", True)),
        "maintenance_mode": bool(dict(features.get("maintenance_mode") or {}).get("enabled", False)),
        "api_key_notice_on_signin": bool(dict(features.get("api_key_notice_on_signin") or {}).get("enabled", False)),
    }


def write_pages(data: Dict[str, Any]) -> None:
    """Persist pages configuration to the runtime settings row."""
    normalized = _normalize_pages_schema(dict(data or {}))
    if not has_app_context():
        raise RuntimeError("write_pages requires an application context")
    from .app_settings_service import ensure_app_settings_row

    row = db.session.get(AppSettings, 1)
    if row is None:
        row = ensure_app_settings_row()

    row.pages_json = normalized
    db.session.commit()

    try:
        eff = dict(current_app.config.get("APP_CONFIG_EFFECTIVE", {}) or {})
        eff["PAGES"] = normalized
        current_app.config["APP_CONFIG_EFFECTIVE"] = eff
    except Exception:
        pass
