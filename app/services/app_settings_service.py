from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Tuple

from flask import current_app, has_app_context
from flask_login import current_user

from ..extensions import db
from ..models import AppSettings, now_utc
from .app_logger import log_warning
from .config_service import (
    DEFAULT_THEME_SETTINGS,
    DEFAULT_VISUAL_SETTINGS,
    load_effective_theme,
    normalize_config_data,
    normalize_theme_settings,
    normalize_visual_settings,
    read_config_json,
)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(dict(out.get(k) or {}), dict(v))
        else:
            out[k] = v
    return out


def _seed_checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _form_bool(form: Any, key: str, default: bool = False) -> bool:
    marker = f"__present__{key}"
    if marker in form:
        raw = str(form.get(key, "")).strip().lower()
        return raw in {"1", "true", "yes", "on", "y"}
    if key not in form:
        return bool(default)
    raw = str(form.get(key, "")).strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def _form_text(form: Any, key: str, default: str = "") -> str:
    value = form.get(key)
    return str(value).strip() if value is not None else str(default)


def _coerce_addon_field_value(raw_value: Any, field_type: str) -> Any:
    ftype = str(field_type or "text").strip().lower()
    if ftype == "checkbox":
        return bool(raw_value)
    if raw_value is None:
        return None
    if ftype == "number":
        raw_text = str(raw_value).strip()
        if not raw_text:
            return ""
        try:
            return int(raw_text) if "." not in raw_text else float(raw_text)
        except Exception:
            return raw_text
    return str(raw_value)


def build_runtime_payload_from_form(
    *,
    form: Any,
    current_config: Dict[str, Any],
    current_theme: Dict[str, Any],
    current_visual: Dict[str, Any],
    addon_config_panels: Iterable[dict[str, Any]] | None = None,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    cfg = normalize_config_data(current_config or {})
    settings = dict(cfg.get("SETTINGS") or {})
    addons = dict(cfg.get("ADDONS") or {})
    items = dict(addons.get("ITEMS") or {})

    security = dict(settings.get("SECURITY") or {})
    logging_cfg = dict(settings.get("LOGGING") or {})
    email = dict(settings.get("EMAIL") or {})
    auth = dict(settings.get("AUTH") or {})
    api = dict(settings.get("API") or {})
    dashboard = dict(settings.get("DASHBOARD") or {})
    addons_config = dict(settings.get("ADDONS_CONFIG") or {})

    settings["APP_NAME"] = _form_text(form, "st_app_name", settings.get("APP_NAME", "WebApp"))
    settings["APP_VERSION"] = _form_text(form, "st_app_version", settings.get("APP_VERSION", "1.0.0"))
    settings["BASE_URL"] = str(settings.get("BASE_URL", "http://127.0.0.1:5000")).strip() or "http://127.0.0.1:5000"

    security["LOGIN_RATE_LIMIT"] = _form_text(form, "sec_login_rate_limit", security.get("LOGIN_RATE_LIMIT", "5 per minute"))
    security["SESSION_TIMEOUT_MIN"] = _form_text(form, "sec_session_timeout_min", security.get("SESSION_TIMEOUT_MIN", 120))
    security["CSRF_ENABLED"] = _form_bool(form, "sec_csrf_enabled", bool(security.get("CSRF_ENABLED", True)))
    security["CSRF_TIME_LIMIT_SEC"] = _form_text(form, "sec_csrf_time_limit_sec", security.get("CSRF_TIME_LIMIT_SEC", 3600))
    security["SECURITY_AUDIT_ENABLED"] = _form_bool(form, "sec_security_audit_enabled", bool(security.get("SECURITY_AUDIT_ENABLED", True)))
    security["BLOCK_TRACE_TRACK"] = _form_bool(form, "sec_block_trace_track", bool(security.get("BLOCK_TRACE_TRACK", True)))
    security["ALLOWED_HOSTS"] = _form_text(form, "sec_allowed_hosts", security.get("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"))
    security["MAX_CONTENT_LENGTH"] = _form_text(form, "sec_max_content_length", security.get("MAX_CONTENT_LENGTH", 16777216))
    security["MAX_FORM_MEMORY_SIZE"] = _form_text(form, "sec_max_form_memory_size", security.get("MAX_FORM_MEMORY_SIZE", 2097152))
    security["API_TOKEN_TOUCH_INTERVAL_SEC"] = _form_text(form, "sec_api_token_touch_interval_sec", security.get("API_TOKEN_TOUCH_INTERVAL_SEC", 30))
    security["MASK_SENSITIVE_LOG_DATA"] = _form_bool(form, "sec_mask_sensitive_log_data", bool(security.get("MASK_SENSITIVE_LOG_DATA", True)))
    security["ALLOW_LOG_TEXT_CONTENT"] = _form_bool(form, "sec_allow_log_text_content", bool(security.get("ALLOW_LOG_TEXT_CONTENT", False)))

    logging_cfg["LEVEL"] = _form_text(form, "log_level", logging_cfg.get("LEVEL", "INFO"))
    logging_cfg["DB_LEVEL"] = _form_text(form, "log_db_level", logging_cfg.get("DB_LEVEL", "INFO"))
    logging_cfg["DB_ENABLED"] = _form_bool(form, "log_db_enabled", bool(logging_cfg.get("DB_ENABLED", True)))
    logging_cfg["FILE"] = _form_text(form, "log_file", logging_cfg.get("FILE", "instance/app.log"))
    logging_cfg["MAX_BYTES"] = _form_text(form, "log_max_bytes", logging_cfg.get("MAX_BYTES", 10485760))
    logging_cfg["BACKUP_COUNT"] = _form_text(form, "log_backup_count", logging_cfg.get("BACKUP_COUNT", 5))
    logging_cfg["MAX_CONTEXT_STRING_LEN"] = _form_text(form, "log_max_context_string_len", logging_cfg.get("MAX_CONTEXT_STRING_LEN", 2000))
    logging_cfg["DB_SKIP_EVENT_PREFIXES"] = _form_text(form, "log_db_skip_event_prefixes", logging_cfg.get("DB_SKIP_EVENT_PREFIXES", "request.completed"))
    logging_cfg["DB_SKIP_LOGGER_PREFIXES"] = _form_text(form, "log_db_skip_logger_prefixes", logging_cfg.get("DB_SKIP_LOGGER_PREFIXES", "sqlalchemy,alembic,werkzeug"))
    logging_cfg["DB_BURST_WINDOW_SEC"] = _form_text(form, "log_db_burst_window_sec", logging_cfg.get("DB_BURST_WINDOW_SEC", 60))
    logging_cfg["DB_BURST_MAX_PER_KEY"] = _form_text(form, "log_db_burst_max_per_key", logging_cfg.get("DB_BURST_MAX_PER_KEY", 6))
    logging_cfg["AUDIT_DB_ENABLED"] = _form_bool(form, "log_audit_db_enabled", bool(logging_cfg.get("AUDIT_DB_ENABLED", True)))
    logging_cfg["AUDIT_DB_LEVEL"] = _form_text(form, "log_audit_db_level", logging_cfg.get("AUDIT_DB_LEVEL", "INFO"))
    logging_cfg["AUDIT_DB_SKIP_EVENT_PREFIXES"] = _form_text(form, "log_audit_db_skip_event_prefixes", logging_cfg.get("AUDIT_DB_SKIP_EVENT_PREFIXES", "page.view,message.read"))
    logging_cfg["AUDIT_DB_BURST_WINDOW_SEC"] = _form_text(form, "log_audit_db_burst_window_sec", logging_cfg.get("AUDIT_DB_BURST_WINDOW_SEC", 60))
    logging_cfg["AUDIT_DB_BURST_MAX_PER_KEY"] = _form_text(form, "log_audit_db_burst_max_per_key", logging_cfg.get("AUDIT_DB_BURST_MAX_PER_KEY", 3))

    email["ENABLED"] = _form_bool(form, "email_enabled", bool(email.get("ENABLED", True)))
    email["MODE"] = _form_text(form, "email_mode", email.get("MODE", "sendmail"))
    email["PUBLIC_BASE_URL"] = _form_text(form, "email_public_base_url", email.get("PUBLIC_BASE_URL", ""))
    email["SENDMAIL_PATH"] = _form_text(form, "email_sendmail_path", email.get("SENDMAIL_PATH", ""))
    email["SMTP_HOST"] = _form_text(form, "email_smtp_host", email.get("SMTP_HOST", ""))
    email["SMTP_PORT"] = _form_text(form, "email_smtp_port", email.get("SMTP_PORT", 587))
    email["SMTP_USERNAME"] = _form_text(form, "email_smtp_username", email.get("SMTP_USERNAME", ""))
    email["SMTP_PASSWORD"] = _form_text(form, "email_smtp_password", email.get("SMTP_PASSWORD", ""))
    email["SMTP_USE_TLS"] = _form_bool(form, "email_smtp_use_tls", bool(email.get("SMTP_USE_TLS", True)))
    email["SMTP_USE_SSL"] = _form_bool(form, "email_smtp_use_ssl", bool(email.get("SMTP_USE_SSL", False)))
    email["SMTP_TIMEOUT_SEC"] = _form_text(form, "email_smtp_timeout_sec", email.get("SMTP_TIMEOUT_SEC", 15))
    email["FROM_EMAIL"] = _form_text(form, "email_from_email", email.get("FROM_EMAIL", "noreply@localhost"))
    email["FROM_NAME"] = _form_text(form, "email_from_name", email.get("FROM_NAME", "WebApp"))
    email["REPLY_TO"] = _form_text(form, "email_reply_to", email.get("REPLY_TO", "noreply@localhost"))
    email["CONFIG_PATH"] = _form_text(form, "email_config_path", email.get("CONFIG_PATH", ""))
    email["CONFIRMATION_TOKEN_TTL_MIN"] = _form_text(form, "email_confirmation_token_ttl_min", email.get("CONFIRMATION_TOKEN_TTL_MIN", 60))
    email["ALLOW_BROADCAST_EMAIL"] = _form_bool(form, "email_allow_broadcast_email", bool(email.get("ALLOW_BROADCAST_EMAIL", True)))
    email["SEND_API_KEY_ON_CONFIRMATION"] = _form_bool(form, "email_send_api_key_on_confirmation", bool(email.get("SEND_API_KEY_ON_CONFIRMATION", False)))

    auth["SELF_REGISTRATION_ENABLED"] = _form_bool(form, "auth_self_registration_enabled", bool(auth.get("SELF_REGISTRATION_ENABLED", True)))
    auth["EMAIL_CONFIRM_REQUIRED"] = _form_bool(form, "auth_email_confirm_required", bool(auth.get("EMAIL_CONFIRM_REQUIRED", True)))
    auth["SEND_API_KEY_NOTICE_ON_SIGNIN"] = _form_bool(form, "auth_send_api_key_notice_on_signin", bool(auth.get("SEND_API_KEY_NOTICE_ON_SIGNIN", False)))
    auth["MAX_FAILED_LOGIN_ATTEMPTS"] = _form_text(form, "auth_max_failed_login_attempts", auth.get("MAX_FAILED_LOGIN_ATTEMPTS", 8))
    auth["LOCKOUT_MINUTES"] = _form_text(form, "auth_lockout_minutes", auth.get("LOCKOUT_MINUTES", 15))
    auth["SIGNUP_RETRY_COOLDOWN_SEC"] = _form_text(form, "auth_signup_retry_cooldown_sec", auth.get("SIGNUP_RETRY_COOLDOWN_SEC", 900))
    auth["MFA_ENABLED"] = _form_bool(form, "auth_mfa_enabled", bool(auth.get("MFA_ENABLED", True)))
    auth["MFA_ISSUER"] = _form_text(form, "auth_mfa_issuer", auth.get("MFA_ISSUER", "WebApp"))
    auth["MFA_RECOVERY_CODES_COUNT"] = _form_text(form, "auth_mfa_recovery_codes_count", auth.get("MFA_RECOVERY_CODES_COUNT", 10))

    api["ENABLED"] = _form_bool(form, "api_enabled", bool(api.get("ENABLED", True)))
    base_url = str(settings.get("BASE_URL", "http://127.0.0.1:5000")).strip().rstrip("/")
    api["PUBLIC_BASE_URL"] = f"{base_url}/api" if base_url else "/api"
    api["DOCS_ENABLED"] = _form_bool(form, "api_docs_enabled", bool(api.get("DOCS_ENABLED", True)))
    api["OPENAPI_ENABLED"] = _form_bool(form, "api_openapi_enabled", bool(api.get("OPENAPI_ENABLED", True)))
    api["REDOC_ENABLED"] = _form_bool(form, "api_redoc_enabled", bool(api.get("REDOC_ENABLED", False)))
    api["DOCS_PATH"] = _form_text(form, "api_docs_path", api.get("DOCS_PATH", "/docs"))
    api["OPENAPI_PATH"] = _form_text(form, "api_openapi_path", api.get("OPENAPI_PATH", "/openapi.json"))
    api["REDOC_PATH"] = _form_text(form, "api_redoc_path", api.get("REDOC_PATH", "/redoc"))
    api["CORS_ALLOWED_ORIGINS"] = _form_text(form, "api_cors_allowed_origins", api.get("CORS_ALLOWED_ORIGINS", ""))

    dashboard["auto_refresh_enabled"] = _form_bool(form, "dash_auto_refresh_enabled", bool(dashboard.get("auto_refresh_enabled", True)))
    dashboard["auto_refresh_sec"] = _form_text(form, "dash_auto_refresh_sec", dashboard.get("auto_refresh_sec", 8))

    addon_ids = {str(k) for k in items.keys()}
    panels = list(addon_config_panels or [])
    for panel in panels:
        addon_id = str(panel.get("addon_id", "")).strip()
        if addon_id:
            addon_ids.add(addon_id)

    for addon_id in sorted(addon_ids):
        current_item = dict(items.get(addon_id) or {})
        items[addon_id] = {
            # Add-on enabled flags must treat an unchecked checkbox as False.
            # Using the generic _form_bool(default=current) keeps disabled add-ons
            # stuck on True because unchecked fields are omitted from POST bodies.
            "enabled": bool(form.get(f"ad_{addon_id}_enabled") in {"1", "true", "yes", "on", "y"}),
            "visibility": str(current_item.get("visibility", "auto") or "auto").strip().lower() or "auto",
        }

    for panel in panels:
        addon_id = str(panel.get("addon_id", "")).strip()
        if not addon_id:
            continue
        current_panel_cfg = dict(addons_config.get(addon_id) or {})
        next_panel_cfg = dict(current_panel_cfg)
        for field in list(panel.get("fields") or []):
            key = str(field.get("key", "")).strip()
            if not key:
                continue
            form_key = f"addon_cfg__{addon_id}__{key}"
            ftype = str(field.get("type", "text")).strip().lower()
            if ftype == "checkbox":
                next_panel_cfg[key] = _coerce_addon_field_value(_form_bool(form, form_key, bool(current_panel_cfg.get(key, field.get("value", False)))), ftype)
            else:
                next_panel_cfg[key] = _coerce_addon_field_value(form.get(form_key, current_panel_cfg.get(key, field.get("value"))), ftype)
        addons_config[addon_id] = next_panel_cfg

    theme = dict(current_theme or {})
    visual = dict(current_visual or {})
    theme["brand_color"] = _form_text(form, "th_brand_color", theme.get("brand_color", "#2563eb"))
    theme["accent_light"] = _form_text(form, "th_accent_light", theme.get("accent_light", "#eff4ff"))
    theme["body_bg"] = _form_text(form, "th_body_bg", theme.get("body_bg", "#f0f4f8"))
    theme["card_bg"] = _form_text(form, "th_card_bg", theme.get("card_bg", "#ffffff"))
    theme["text_color"] = _form_text(form, "th_text_color", theme.get("text_color", "#1e293b"))
    theme["text_muted"] = _form_text(form, "th_text_muted", theme.get("text_muted", "#6b7a8d"))
    theme["topbar_bg"] = _form_text(form, "th_topbar_bg", theme.get("topbar_bg", "#0f1923"))
    theme["sidebar_bg"] = _form_text(form, "th_sidebar_bg", theme.get("sidebar_bg", "#0f1923"))
    theme["sidebar_width_px"] = _form_text(form, "th_sidebar_width_px", theme.get("sidebar_width_px", 260))
    theme["topbar_height_px"] = _form_text(form, "th_topbar_height_px", theme.get("topbar_height_px", 56))
    theme["radius_px"] = _form_text(form, "th_radius_px", theme.get("radius_px", 10))
    theme["font_size_base_px"] = _form_text(form, "th_font_size_base_px", theme.get("font_size_base_px", 14))

    dash_visual = dict(visual.get("DASHBOARD") or {})
    dash_visual["recent_events_max"] = _form_text(form, "vis_recent_events_max", dash_visual.get("recent_events_max", 10))
    dash_visual["recent_events_max_height_px"] = _form_text(form, "vis_recent_events_max_height_px", dash_visual.get("recent_events_max_height_px", 320))
    kpi_max = dict(dash_visual.get("kpi_max") or {})
    for key, default in {"users": 100, "messages": 200, "logins": 100, "logs": 500, "db_mb": 1024}.items():
        kpi_max[key] = _form_text(form, f"vis_kpi_{key}", kpi_max.get(key, default))
    dash_visual["kpi_max"] = kpi_max
    visual["DASHBOARD"] = dash_visual

    settings["SECURITY"] = security
    settings["LOGGING"] = logging_cfg
    settings["EMAIL"] = email
    settings["AUTH"] = auth
    settings["API"] = api
    settings["DASHBOARD"] = dashboard
    settings["ADDONS_CONFIG"] = addons_config
    addons["ITEMS"] = items

    config = {"SETTINGS": settings, "ADDONS": addons}
    if isinstance(cfg.get("PAGES"), dict):
        config["PAGES"] = dict(cfg.get("PAGES") or {})
    return normalize_config_data(config), normalize_theme_settings(theme), normalize_visual_settings(visual)


def build_settings_export_payload(row: AppSettings) -> dict[str, Any]:
    return {
        "meta": {
            "app": row.app_name,
            "version": row.app_version,
            "revision": int(row.revision or 1),
            "exported_at_utc": now_utc().isoformat() + "Z",
            "seed_source": row.seed_source,
            "seed_checksum": row.seed_checksum,
        },
        "settings": {
            "app_name": row.app_name,
            "app_version": row.app_version,
            "base_url": row.base_url,
            "settings": dict(row.settings_json or {}),
            "addons": dict(row.addons_json or {}),
            "pages": dict(row.pages_json or {}) if isinstance(row.pages_json, dict) else {},
            "theme": dict(row.theme_json or {}),
            "visual": dict(row.visual_json or {}),
        },
    }


def _apply_runtime_payload(
    row: AppSettings,
    *,
    config: Dict[str, Any],
    theme: Dict[str, Any],
    visual: Dict[str, Any],
    seed_source: str | None = None,
    seed_checksum: str | None = None,
    mark_imported: bool = False,
) -> None:
    normalized = normalize_config_data(config)
    settings_src = dict(normalized.get("SETTINGS") or {})

    row.app_name = str(settings_src.get("APP_NAME", "WebApp")).strip() or "WebApp"
    row.app_version = str(settings_src.get("APP_VERSION", "1.0.0")).strip() or "1.0.0"
    row.base_url = str(settings_src.get("BASE_URL", "http://127.0.0.1:5000")).strip() or "http://127.0.0.1:5000"
    row.settings_json = {
        key: val
        for key, val in settings_src.items()
        if key not in {"APP_NAME", "APP_VERSION", "BASE_URL"}
    }
    row.addons_json = dict(normalized.get("ADDONS") or {})
    row.pages_json = dict(normalized.get("PAGES") or {}) if isinstance(normalized.get("PAGES"), dict) else None
    row.theme_json = normalize_theme_settings(theme)
    row.visual_json = normalize_visual_settings(visual)
    row.revision = int(row.revision or 0) + 1
    if seed_source is not None:
        row.seed_source = seed_source
    if seed_checksum is not None:
        row.seed_checksum = seed_checksum
    if mark_imported:
        row.last_imported_at = now_utc()


def ensure_app_settings_row() -> AppSettings:
    """Ensure there is one runtime settings row, seeded from app_config.json."""
    row = db.session.get(AppSettings, 1)
    if row is not None:
        return row

    base_config = read_config_json()
    normalized = normalize_config_data(base_config)
    theme_from_file = base_config.get("THEME") if isinstance(base_config.get("THEME"), dict) else {}
    visual_from_file = base_config.get("VISUAL") if isinstance(base_config.get("VISUAL"), dict) else {}

    row = AppSettings(id=1)
    _apply_runtime_payload(
        row,
        config=normalized,
        theme=load_effective_theme(theme_from_file or {}),
        visual=normalize_visual_settings(_deep_merge(DEFAULT_VISUAL_SETTINGS, visual_from_file or {})),
        seed_source=str(current_app.config.get("CONFIG_PATH", "app_config.json")) if has_app_context() else "app_config.json",
        seed_checksum=_seed_checksum(base_config if isinstance(base_config, dict) else {}),
    )
    row.revision = 1
    db.session.add(row)
    db.session.commit()
    return row


def get_app_settings_raw() -> AppSettings:
    try:
        return ensure_app_settings_row()
    except Exception:
        base_config = normalize_config_data(read_config_json())
        theme = load_effective_theme({})
        visual = normalize_visual_settings(DEFAULT_VISUAL_SETTINGS)
        row = AppSettings(id=1)
        _apply_runtime_payload(
            row,
            config=base_config,
            theme=theme,
            visual=visual,
            seed_source="app_config.json",
            seed_checksum=_seed_checksum(base_config),
        )
        row.revision = 1
        return row


def get_effective_settings() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    base_config = normalize_config_data({})
    base_theme = load_effective_theme(DEFAULT_THEME_SETTINGS)
    base_visual = normalize_visual_settings(DEFAULT_VISUAL_SETTINGS)

    row = get_app_settings_raw()

    cfg = normalize_config_data(_deep_merge(base_config, dict(row.config_json or {})))
    theme = normalize_theme_settings(_deep_merge(base_theme, dict(row.theme_json or {})))
    visual = normalize_visual_settings(_deep_merge(base_visual, dict(row.visual_json or {})))

    return cfg, theme, visual


def _apply_runtime_to_app(
    *,
    config: Dict[str, Any],
    theme: Dict[str, Any],
    visual: Dict[str, Any],
    row: AppSettings,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    effective = normalize_config_data(dict(config or {}))
    theme_eff = normalize_theme_settings(dict(theme or {}))
    visual_eff = normalize_visual_settings(dict(visual or {}))

    current_app.config["APP_CONFIG_EFFECTIVE"] = effective
    current_app.config["THEME"] = theme_eff
    current_app.config["VISUAL"] = visual_eff

    settings_cfg = dict(effective.get("SETTINGS", {}) or {})
    addons_cfg = dict(effective.get("ADDONS", {}) or {})
    sec_cfg = dict(settings_cfg.get("SECURITY", {}) or {})
    email_cfg = dict(settings_cfg.get("EMAIL", {}) or {})
    auth_cfg = dict(settings_cfg.get("AUTH", {}) or {})
    dashboard_cfg = dict(settings_cfg.get("DASHBOARD", {}) or {})
    addon_settings_cfg = dict(settings_cfg.get("ADDONS_CONFIG", {}) or {})
    api_cfg = dict(settings_cfg.get("API") or {})
    api_access_cfg = dict(settings_cfg.get("API_ACCESS") or {})

    current_app.config.update(
        APP_NAME=row.app_name,
        APP_VERSION=row.app_version,
        BASE_URL=row.base_url,
        SETTINGS=settings_cfg,
        SECURITY=sec_cfg,
        LOGGING=dict(settings_cfg.get("LOGGING") or {}),
        EMAIL=email_cfg,
        AUTH=auth_cfg,
        API=api_cfg,
        API_ACCESS=api_access_cfg,
        ADDONS=addons_cfg,
        ADDON_POLICIES=dict((addons_cfg.get("ITEMS") or {})) if isinstance(addons_cfg.get("ITEMS"), dict) else {},
        ADDON_SETTINGS=addon_settings_cfg,
        DASHBOARD=dashboard_cfg,
    )
    return effective, theme_eff, visual_eff


def update_settings(*, config: Dict[str, Any], theme: Dict[str, Any], visual: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    row = ensure_app_settings_row()
    try:
        previous_revision = int(row.revision or 1)
        _apply_runtime_payload(row, config=config, theme=theme, visual=visual)
        if previous_revision <= 0:
            row.revision = 1
        if getattr(current_user, "is_authenticated", False):
            row.updated_by_user_id = int(current_user.id)
        db.session.add(row)
        db.session.commit()
        db.session.refresh(row)
        effective, theme_eff, visual_eff = _apply_runtime_to_app(
            config=row.config_json,
            theme=dict(row.theme_json or {}),
            visual=dict(row.visual_json or {}),
            row=row,
        )
    except Exception:
        db.session.rollback()
        raise

    try:
        from ..logging_setup import configure_logging

        configure_logging(current_app)
    except Exception as exc:
        log_warning(
            "settings.logging_reconfigure_failed",
            "Failed to reconfigure logging after settings update",
            context={"error": str(exc)[:240]},
        )
    return effective, theme_eff, visual_eff


def import_settings_payload(data: dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    payload = dict(data or {})
    body = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload

    app_name = str(body.get("app_name", "WebApp")).strip() or "WebApp"
    app_version = str(body.get("app_version", "1.0.0")).strip() or "1.0.0"
    base_url = str(body.get("base_url", "http://127.0.0.1:5000")).strip() or "http://127.0.0.1:5000"
    settings = dict(body.get("settings") or {})
    addons = dict(body.get("addons") or {})
    pages = dict(body.get("pages") or {}) if isinstance(body.get("pages"), dict) else {}
    theme = dict(body.get("theme") or {})
    visual = dict(body.get("visual") or {})

    config = {
        "SETTINGS": {
            "APP_NAME": app_name,
            "APP_VERSION": app_version,
            "BASE_URL": base_url,
            **settings,
        },
        "ADDONS": addons,
    }
    if pages:
        config["PAGES"] = pages
    return normalize_config_data(config), normalize_theme_settings(theme), normalize_visual_settings(visual)
