from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_SECURITY_SETTINGS: dict[str, Any] = {
    "LOGIN_RATE_LIMIT": "5 per minute",
    "SESSION_TIMEOUT_MIN": 120,
    "CSRF_ENABLED": True,
    "CSRF_TIME_LIMIT_SEC": 3600,
    "SECURITY_AUDIT_ENABLED": True,
    "BLOCK_TRACE_TRACK": True,
    "ALLOWED_HOSTS": "localhost,127.0.0.1,[::1]",
    "MAX_CONTENT_LENGTH": 16777216,
    "MAX_FORM_MEMORY_SIZE": 2097152,
    "API_TOKEN_TOUCH_INTERVAL_SEC": 30,
    "MASK_SENSITIVE_LOG_DATA": True,
    "ALLOW_LOG_TEXT_CONTENT": False,
}

DEFAULT_LOGGING_SETTINGS: dict[str, Any] = {
    "LEVEL": "INFO",
    "DB_LEVEL": "INFO",
    "DB_ENABLED": True,
    "FILE": "instance/app.log",
    "MAX_BYTES": 10485760,
    "BACKUP_COUNT": 5,
    "MAX_CONTEXT_STRING_LEN": 2000,
    "DB_SKIP_EVENT_PREFIXES": "request.completed",
    "DB_SKIP_LOGGER_PREFIXES": "sqlalchemy,alembic,werkzeug",
    "DB_BURST_WINDOW_SEC": 60,
    "DB_BURST_MAX_PER_KEY": 6,
    "AUDIT_DB_ENABLED": True,
    "AUDIT_DB_LEVEL": "INFO",
    "AUDIT_DB_SKIP_EVENT_PREFIXES": "page.view,message.read",
    "AUDIT_DB_BURST_WINDOW_SEC": 60,
    "AUDIT_DB_BURST_MAX_PER_KEY": 3,
}

DEFAULT_EMAIL_SETTINGS: dict[str, Any] = {
    "ENABLED": True,
    "MODE": "sendmail",
    "PUBLIC_BASE_URL": "",
    "SENDMAIL_PATH": "",
    "SMTP_HOST": "",
    "SMTP_PORT": 587,
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_USE_TLS": True,
    "SMTP_USE_SSL": False,
    "SMTP_TIMEOUT_SEC": 15,
    "FROM_EMAIL": "noreply@localhost",
    "FROM_NAME": "WebApp",
    "REPLY_TO": "noreply@localhost",
    "CONFIG_PATH": "",
    "CONFIRMATION_TOKEN_TTL_MIN": 60,
    "ALLOW_BROADCAST_EMAIL": True,
    "SEND_API_KEY_ON_CONFIRMATION": False,
}

DEFAULT_AUTH_SETTINGS: dict[str, Any] = {
    "SELF_REGISTRATION_ENABLED": True,
    "EMAIL_CONFIRM_REQUIRED": True,
    "SEND_API_KEY_NOTICE_ON_SIGNIN": False,
    "MAX_FAILED_LOGIN_ATTEMPTS": 8,
    "LOCKOUT_MINUTES": 15,
    "SIGNUP_RETRY_COOLDOWN_SEC": 900,
    "MFA_ENABLED": True,
    "MFA_ISSUER": "WebApp",
    "MFA_RECOVERY_CODES_COUNT": 10,
}

DEFAULT_API_SETTINGS: dict[str, Any] = {
    "ENABLED": True,
    "PUBLIC_BASE_URL": "",
    "DOCS_ENABLED": True,
    "OPENAPI_ENABLED": True,
    "REDOC_ENABLED": False,
    "DOCS_PATH": "/docs",
    "OPENAPI_PATH": "/openapi.json",
    "REDOC_PATH": "/redoc",
    "CORS_ALLOWED_ORIGINS": "",
}

DEFAULT_DASHBOARD_SETTINGS: dict[str, Any] = {
    "auto_refresh_enabled": True,
    "auto_refresh_sec": 8,
}

DEFAULT_THEME_SETTINGS: dict[str, Any] = {
    "brand_color": "#2563eb",
    "accent_light": "#eff4ff",
    "sidebar_width_px": 260,
    "topbar_height_px": 56,
    "radius_px": 10,
    "font_family_base": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif",
    "font_family_mono": "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New, monospace",
    "font_size_base_px": 14,
    "line_height_base": 1.45,
    "page_title_font_size_rem": 1.26,
    "card_header_font_size_rem": 0.80,
    "button_font_size_rem": 0.82,
    "button_padding_y_rem": 0.24,
    "button_padding_x_rem": 0.58,
    "button_sm_font_size_rem": 0.79,
    "button_sm_padding_y_rem": 0.20,
    "button_sm_padding_x_rem": 0.48,
    "body_bg": "#f0f4f8",
    "text_color": "#1e293b",
    "text_muted": "#6b7a8d",
    "card_bg": "#ffffff",
    "card_border": "#e5eaf0",
    "topbar_bg": "#0f1923",
    "topbar_border": "#1e2f40",
    "sidebar_bg": "#0f1923",
    "sidebar_text": "#8fa3b8",
    "sidebar_active": "#ffffff",
    "sidebar_section": "#4a6080",
    "sidebar_hover_bg": "rgba(255,255,255,.06)",
    "success": "#16a34a",
    "warning": "#d97706",
    "danger": "#dc2626",
    "info": "#0891b2",
}

DEFAULT_VISUAL_SETTINGS: dict[str, Any] = {
    "DASHBOARD": {
        "recent_events_max": 10,
        "recent_events_max_height_px": 320,
        "kpi_max": {
            "users": 100,
            "messages": 200,
            "logins": 100,
            "logs": 500,
            "db_mb": 1024,
        },
    }
}

_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|(?:rgb|rgba|hsl|hsla)\([0-9.,%\s]+\))$")
_FONT_RE = re.compile(r'^[a-zA-Z0-9 ,.\-_"\'()+/]{1,180}$')
_ICON_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,39}$")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(dict(out[key]), dict(value))
        else:
            out[key] = value
    return out


def _norm_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _norm_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        out = int(str(value).strip())
    except Exception:
        out = int(default)
    return max(min_value, min(max_value, out))


def _norm_float(value: Any, default: float, min_value: float, max_value: float) -> float:
    try:
        out = float(str(value).strip())
    except Exception:
        out = float(default)
    return max(min_value, min(max_value, out))


def _norm_text(value: Any, default: str, max_len: int = 180) -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    return raw[:max_len]


def _norm_color(value: Any, default: str) -> str:
    raw = str(value or "").strip()
    return raw if _COLOR_RE.match(raw) else default


def _norm_font(value: Any, default: str) -> str:
    raw = str(value or "").strip()
    return raw if raw and _FONT_RE.match(raw) else default


def _norm_icon(value: Any, default: str) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw and _ICON_RE.match(raw) else default


def normalize_theme_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = dict(raw or {})
    out = dict(DEFAULT_THEME_SETTINGS)
    for key in (
        "brand_color",
        "accent_light",
        "body_bg",
        "text_color",
        "text_muted",
        "card_bg",
        "card_border",
        "topbar_bg",
        "topbar_border",
        "sidebar_bg",
        "sidebar_text",
        "sidebar_active",
        "sidebar_section",
        "sidebar_hover_bg",
        "success",
        "warning",
        "danger",
        "info",
    ):
        out[key] = _norm_color(src.get(key), str(DEFAULT_THEME_SETTINGS[key]))
    out["font_family_base"] = _norm_font(src.get("font_family_base"), str(DEFAULT_THEME_SETTINGS["font_family_base"]))
    out["font_family_mono"] = _norm_font(src.get("font_family_mono"), str(DEFAULT_THEME_SETTINGS["font_family_mono"]))
    out["sidebar_width_px"] = _norm_int(src.get("sidebar_width_px"), 260, 220, 360)
    out["topbar_height_px"] = _norm_int(src.get("topbar_height_px"), 56, 48, 88)
    out["radius_px"] = _norm_int(src.get("radius_px"), 10, 6, 18)
    out["font_size_base_px"] = _norm_int(src.get("font_size_base_px"), 14, 12, 18)
    out["line_height_base"] = round(_norm_float(src.get("line_height_base"), 1.45, 1.2, 1.8), 2)
    out["page_title_font_size_rem"] = round(_norm_float(src.get("page_title_font_size_rem"), 1.26, 1.0, 2.0), 2)
    out["card_header_font_size_rem"] = round(_norm_float(src.get("card_header_font_size_rem"), 0.80, 0.7, 1.1), 2)
    out["button_font_size_rem"] = round(_norm_float(src.get("button_font_size_rem"), 0.82, 0.7, 1.0), 2)
    out["button_padding_y_rem"] = round(_norm_float(src.get("button_padding_y_rem"), 0.24, 0.1, 0.6), 2)
    out["button_padding_x_rem"] = round(_norm_float(src.get("button_padding_x_rem"), 0.58, 0.2, 1.0), 2)
    out["button_sm_font_size_rem"] = round(_norm_float(src.get("button_sm_font_size_rem"), 0.79, 0.7, 1.0), 2)
    out["button_sm_padding_y_rem"] = round(_norm_float(src.get("button_sm_padding_y_rem"), 0.20, 0.1, 0.5), 2)
    out["button_sm_padding_x_rem"] = round(_norm_float(src.get("button_sm_padding_x_rem"), 0.48, 0.2, 0.9), 2)
    return out


def normalize_visual_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = dict(raw or {})
    dashboard = dict(DEFAULT_VISUAL_SETTINGS["DASHBOARD"])
    incoming = dict(src.get("DASHBOARD") or {})
    dashboard["recent_events_max"] = _norm_int(incoming.get("recent_events_max"), 10, 4, 50)
    dashboard["recent_events_max_height_px"] = _norm_int(incoming.get("recent_events_max_height_px"), 320, 180, 900)
    kpis = dict(dashboard.get("kpi_max") or {})
    incoming_kpis = dict(incoming.get("kpi_max") or {})
    for key, default in kpis.items():
        kpis[key] = _norm_int(incoming_kpis.get(key), int(default), 1, 100000)
    dashboard["kpi_max"] = kpis
    return {"DASHBOARD": dashboard}


def load_effective_theme(config_theme: dict[str, Any] | None) -> dict[str, Any]:
    return normalize_theme_settings(_deep_merge(DEFAULT_THEME_SETTINGS, dict(config_theme or {})))


def normalize_config_data(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw or {})
    settings_src = dict(data.get("SETTINGS") or {})
    addons_src = dict(data.get("ADDONS") or {})

    security_src = dict(settings_src.get("SECURITY") or {})
    logging_src = dict(settings_src.get("LOGGING") or {})
    email_src = dict(settings_src.get("EMAIL") or {})
    auth_src = dict(settings_src.get("AUTH") or {})
    api_src = dict(settings_src.get("API") or {})
    dashboard_src = dict(settings_src.get("DASHBOARD") or {})

    security = {
        "LOGIN_RATE_LIMIT": _norm_text(security_src.get("LOGIN_RATE_LIMIT"), str(DEFAULT_SECURITY_SETTINGS["LOGIN_RATE_LIMIT"]), 40),
        "SESSION_TIMEOUT_MIN": _norm_int(security_src.get("SESSION_TIMEOUT_MIN"), int(DEFAULT_SECURITY_SETTINGS["SESSION_TIMEOUT_MIN"]), 5, 43200),
        "CSRF_ENABLED": _norm_bool(security_src.get("CSRF_ENABLED"), bool(DEFAULT_SECURITY_SETTINGS["CSRF_ENABLED"])),
        "CSRF_TIME_LIMIT_SEC": _norm_int(security_src.get("CSRF_TIME_LIMIT_SEC"), int(DEFAULT_SECURITY_SETTINGS["CSRF_TIME_LIMIT_SEC"]), 60, 86400),
        "SECURITY_AUDIT_ENABLED": _norm_bool(security_src.get("SECURITY_AUDIT_ENABLED"), bool(DEFAULT_SECURITY_SETTINGS["SECURITY_AUDIT_ENABLED"])),
        "BLOCK_TRACE_TRACK": _norm_bool(security_src.get("BLOCK_TRACE_TRACK"), bool(DEFAULT_SECURITY_SETTINGS["BLOCK_TRACE_TRACK"])),
        "ALLOWED_HOSTS": _norm_text(security_src.get("ALLOWED_HOSTS"), str(DEFAULT_SECURITY_SETTINGS["ALLOWED_HOSTS"]), 500),
        "MAX_CONTENT_LENGTH": _norm_int(security_src.get("MAX_CONTENT_LENGTH"), int(DEFAULT_SECURITY_SETTINGS["MAX_CONTENT_LENGTH"]), 1024, 536870912),
        "MAX_FORM_MEMORY_SIZE": _norm_int(security_src.get("MAX_FORM_MEMORY_SIZE"), int(DEFAULT_SECURITY_SETTINGS["MAX_FORM_MEMORY_SIZE"]), 1024, 134217728),
        "API_TOKEN_TOUCH_INTERVAL_SEC": _norm_int(security_src.get("API_TOKEN_TOUCH_INTERVAL_SEC"), int(DEFAULT_SECURITY_SETTINGS["API_TOKEN_TOUCH_INTERVAL_SEC"]), 1, 3600),
        "MASK_SENSITIVE_LOG_DATA": _norm_bool(security_src.get("MASK_SENSITIVE_LOG_DATA"), bool(DEFAULT_SECURITY_SETTINGS["MASK_SENSITIVE_LOG_DATA"])),
        "ALLOW_LOG_TEXT_CONTENT": _norm_bool(security_src.get("ALLOW_LOG_TEXT_CONTENT"), bool(DEFAULT_SECURITY_SETTINGS["ALLOW_LOG_TEXT_CONTENT"])),
    }

    logging = {
        "LEVEL": _norm_text(logging_src.get("LEVEL"), str(DEFAULT_LOGGING_SETTINGS["LEVEL"]), 16).upper(),
        "DB_LEVEL": _norm_text(logging_src.get("DB_LEVEL"), str(DEFAULT_LOGGING_SETTINGS["DB_LEVEL"]), 16).upper(),
        "DB_ENABLED": _norm_bool(logging_src.get("DB_ENABLED"), bool(DEFAULT_LOGGING_SETTINGS["DB_ENABLED"])),
        "FILE": _norm_text(logging_src.get("FILE"), str(DEFAULT_LOGGING_SETTINGS["FILE"]), 220),
        "MAX_BYTES": _norm_int(logging_src.get("MAX_BYTES"), int(DEFAULT_LOGGING_SETTINGS["MAX_BYTES"]), 65536, 1073741824),
        "BACKUP_COUNT": _norm_int(logging_src.get("BACKUP_COUNT"), int(DEFAULT_LOGGING_SETTINGS["BACKUP_COUNT"]), 1, 100),
        "MAX_CONTEXT_STRING_LEN": _norm_int(logging_src.get("MAX_CONTEXT_STRING_LEN"), int(DEFAULT_LOGGING_SETTINGS["MAX_CONTEXT_STRING_LEN"]), 120, 12000),
        "DB_SKIP_EVENT_PREFIXES": _norm_text(logging_src.get("DB_SKIP_EVENT_PREFIXES"), str(DEFAULT_LOGGING_SETTINGS["DB_SKIP_EVENT_PREFIXES"]), 500),
        "DB_SKIP_LOGGER_PREFIXES": _norm_text(logging_src.get("DB_SKIP_LOGGER_PREFIXES"), str(DEFAULT_LOGGING_SETTINGS["DB_SKIP_LOGGER_PREFIXES"]), 500),
        "DB_BURST_WINDOW_SEC": _norm_int(logging_src.get("DB_BURST_WINDOW_SEC"), int(DEFAULT_LOGGING_SETTINGS["DB_BURST_WINDOW_SEC"]), 5, 3600),
        "DB_BURST_MAX_PER_KEY": _norm_int(logging_src.get("DB_BURST_MAX_PER_KEY"), int(DEFAULT_LOGGING_SETTINGS["DB_BURST_MAX_PER_KEY"]), 1, 100),
        "AUDIT_DB_ENABLED": _norm_bool(logging_src.get("AUDIT_DB_ENABLED"), bool(DEFAULT_LOGGING_SETTINGS["AUDIT_DB_ENABLED"])),
        "AUDIT_DB_LEVEL": _norm_text(logging_src.get("AUDIT_DB_LEVEL"), str(DEFAULT_LOGGING_SETTINGS["AUDIT_DB_LEVEL"]), 16).upper(),
        "AUDIT_DB_SKIP_EVENT_PREFIXES": _norm_text(logging_src.get("AUDIT_DB_SKIP_EVENT_PREFIXES"), str(DEFAULT_LOGGING_SETTINGS["AUDIT_DB_SKIP_EVENT_PREFIXES"]), 500),
        "AUDIT_DB_BURST_WINDOW_SEC": _norm_int(logging_src.get("AUDIT_DB_BURST_WINDOW_SEC"), int(DEFAULT_LOGGING_SETTINGS["AUDIT_DB_BURST_WINDOW_SEC"]), 5, 3600),
        "AUDIT_DB_BURST_MAX_PER_KEY": _norm_int(logging_src.get("AUDIT_DB_BURST_MAX_PER_KEY"), int(DEFAULT_LOGGING_SETTINGS["AUDIT_DB_BURST_MAX_PER_KEY"]), 1, 100),
    }

    email = {
        "ENABLED": _norm_bool(email_src.get("ENABLED"), bool(DEFAULT_EMAIL_SETTINGS["ENABLED"])),
        "MODE": _norm_text(email_src.get("MODE"), str(DEFAULT_EMAIL_SETTINGS["MODE"]), 32),
        "PUBLIC_BASE_URL": _norm_text(email_src.get("PUBLIC_BASE_URL"), str(DEFAULT_EMAIL_SETTINGS["PUBLIC_BASE_URL"]), 220),
        "SENDMAIL_PATH": _norm_text(email_src.get("SENDMAIL_PATH"), str(DEFAULT_EMAIL_SETTINGS["SENDMAIL_PATH"]), 220),
        "SMTP_HOST": _norm_text(email_src.get("SMTP_HOST"), str(DEFAULT_EMAIL_SETTINGS["SMTP_HOST"]), 220),
        "SMTP_PORT": _norm_int(email_src.get("SMTP_PORT"), int(DEFAULT_EMAIL_SETTINGS["SMTP_PORT"]), 1, 65535),
        "SMTP_USERNAME": _norm_text(email_src.get("SMTP_USERNAME"), str(DEFAULT_EMAIL_SETTINGS["SMTP_USERNAME"]), 180),
        "SMTP_PASSWORD": _norm_text(email_src.get("SMTP_PASSWORD"), str(DEFAULT_EMAIL_SETTINGS["SMTP_PASSWORD"]), 300),
        "SMTP_USE_TLS": _norm_bool(email_src.get("SMTP_USE_TLS"), bool(DEFAULT_EMAIL_SETTINGS["SMTP_USE_TLS"])),
        "SMTP_USE_SSL": _norm_bool(email_src.get("SMTP_USE_SSL"), bool(DEFAULT_EMAIL_SETTINGS["SMTP_USE_SSL"])),
        "SMTP_TIMEOUT_SEC": _norm_int(email_src.get("SMTP_TIMEOUT_SEC"), int(DEFAULT_EMAIL_SETTINGS["SMTP_TIMEOUT_SEC"]), 3, 120),
        "FROM_EMAIL": _norm_text(email_src.get("FROM_EMAIL"), str(DEFAULT_EMAIL_SETTINGS["FROM_EMAIL"]), 180),
        "FROM_NAME": _norm_text(email_src.get("FROM_NAME"), str(DEFAULT_EMAIL_SETTINGS["FROM_NAME"]), 120),
        "REPLY_TO": _norm_text(email_src.get("REPLY_TO"), str(DEFAULT_EMAIL_SETTINGS["REPLY_TO"]), 180),
        "CONFIG_PATH": _norm_text(email_src.get("CONFIG_PATH"), str(DEFAULT_EMAIL_SETTINGS["CONFIG_PATH"]), 220),
        "CONFIRMATION_TOKEN_TTL_MIN": _norm_int(email_src.get("CONFIRMATION_TOKEN_TTL_MIN"), int(DEFAULT_EMAIL_SETTINGS["CONFIRMATION_TOKEN_TTL_MIN"]), 1, 10080),
        "ALLOW_BROADCAST_EMAIL": _norm_bool(email_src.get("ALLOW_BROADCAST_EMAIL"), bool(DEFAULT_EMAIL_SETTINGS["ALLOW_BROADCAST_EMAIL"])),
        "SEND_API_KEY_ON_CONFIRMATION": _norm_bool(email_src.get("SEND_API_KEY_ON_CONFIRMATION"), bool(DEFAULT_EMAIL_SETTINGS["SEND_API_KEY_ON_CONFIRMATION"])),
    }

    auth = {
        "SELF_REGISTRATION_ENABLED": _norm_bool(auth_src.get("SELF_REGISTRATION_ENABLED"), bool(DEFAULT_AUTH_SETTINGS["SELF_REGISTRATION_ENABLED"])),
        "EMAIL_CONFIRM_REQUIRED": _norm_bool(auth_src.get("EMAIL_CONFIRM_REQUIRED"), bool(DEFAULT_AUTH_SETTINGS["EMAIL_CONFIRM_REQUIRED"])),
        "SEND_API_KEY_NOTICE_ON_SIGNIN": _norm_bool(auth_src.get("SEND_API_KEY_NOTICE_ON_SIGNIN"), bool(DEFAULT_AUTH_SETTINGS["SEND_API_KEY_NOTICE_ON_SIGNIN"])),
        "MAX_FAILED_LOGIN_ATTEMPTS": _norm_int(auth_src.get("MAX_FAILED_LOGIN_ATTEMPTS"), int(DEFAULT_AUTH_SETTINGS["MAX_FAILED_LOGIN_ATTEMPTS"]), 1, 100),
        "LOCKOUT_MINUTES": _norm_int(auth_src.get("LOCKOUT_MINUTES"), int(DEFAULT_AUTH_SETTINGS["LOCKOUT_MINUTES"]), 1, 10080),
        "SIGNUP_RETRY_COOLDOWN_SEC": _norm_int(auth_src.get("SIGNUP_RETRY_COOLDOWN_SEC"), int(DEFAULT_AUTH_SETTINGS["SIGNUP_RETRY_COOLDOWN_SEC"]), 10, 86400),
        "MFA_ENABLED": _norm_bool(auth_src.get("MFA_ENABLED"), bool(DEFAULT_AUTH_SETTINGS["MFA_ENABLED"])),
        "MFA_ISSUER": _norm_text(auth_src.get("MFA_ISSUER"), str(DEFAULT_AUTH_SETTINGS["MFA_ISSUER"]), 120),
        "MFA_RECOVERY_CODES_COUNT": _norm_int(auth_src.get("MFA_RECOVERY_CODES_COUNT"), int(DEFAULT_AUTH_SETTINGS["MFA_RECOVERY_CODES_COUNT"]), 1, 50),
    }

    api = {
        "ENABLED": _norm_bool(api_src.get("ENABLED"), bool(DEFAULT_API_SETTINGS["ENABLED"])),
        "PUBLIC_BASE_URL": _norm_text(api_src.get("PUBLIC_BASE_URL"), str(DEFAULT_API_SETTINGS["PUBLIC_BASE_URL"]), 220),
        "DOCS_ENABLED": _norm_bool(api_src.get("DOCS_ENABLED"), bool(DEFAULT_API_SETTINGS["DOCS_ENABLED"])),
        "OPENAPI_ENABLED": _norm_bool(api_src.get("OPENAPI_ENABLED"), bool(DEFAULT_API_SETTINGS["OPENAPI_ENABLED"])),
        "REDOC_ENABLED": _norm_bool(api_src.get("REDOC_ENABLED"), bool(DEFAULT_API_SETTINGS["REDOC_ENABLED"])),
        "DOCS_PATH": _norm_text(api_src.get("DOCS_PATH"), str(DEFAULT_API_SETTINGS["DOCS_PATH"]), 64),
        "OPENAPI_PATH": _norm_text(api_src.get("OPENAPI_PATH"), str(DEFAULT_API_SETTINGS["OPENAPI_PATH"]), 64),
        "REDOC_PATH": _norm_text(api_src.get("REDOC_PATH"), str(DEFAULT_API_SETTINGS["REDOC_PATH"]), 64),
        "CORS_ALLOWED_ORIGINS": _norm_text(api_src.get("CORS_ALLOWED_ORIGINS"), str(DEFAULT_API_SETTINGS["CORS_ALLOWED_ORIGINS"]), 500),
    }

    dashboard = {
        "auto_refresh_enabled": _norm_bool(dashboard_src.get("auto_refresh_enabled"), bool(DEFAULT_DASHBOARD_SETTINGS["auto_refresh_enabled"])),
        "auto_refresh_sec": _norm_int(dashboard_src.get("auto_refresh_sec"), int(DEFAULT_DASHBOARD_SETTINGS["auto_refresh_sec"]), 3, 3600),
    }

    settings = {
        "APP_NAME": _norm_text(settings_src.get("APP_NAME"), "WebApp", 80),
        "APP_VERSION": _norm_text(settings_src.get("APP_VERSION"), "1.0.0", 32),
        "BASE_URL": _norm_text(settings_src.get("BASE_URL"), "http://127.0.0.1:5000", 220),
        "UI_LANGUAGE": "it" if str(settings_src.get("UI_LANGUAGE", "en")).strip().lower().startswith("it") else "en",
        "SECURITY": security,
        "LOGGING": logging,
        "EMAIL": email,
        "AUTH": auth,
        "API": api,
        "DASHBOARD": dashboard,
        "ADDONS_CONFIG": dict(settings_src.get("ADDONS_CONFIG") or {}),
    }

    items: dict[str, Any] = {}
    for key, value in dict(addons_src.get("ITEMS") or {}).items():
        if not isinstance(value, dict):
            continue
        items[str(key)] = {
            "enabled": _norm_bool(value.get("enabled", True), True),
            "display_name": _norm_text(value.get("display_name"), "", 80),
            "icon": _norm_icon(value.get("icon"), ""),
            "visibility": _norm_text(value.get("visibility"), "auto", 16).lower() or "auto",
        }

    normalized = {
        "SETTINGS": settings,
        "ADDONS": {"ITEMS": items},
    }
    if isinstance(data.get("PAGES"), dict):
        normalized["PAGES"] = dict(data.get("PAGES") or {})
    return normalized


def read_config_json() -> dict[str, Any]:
    config_path = str(os.getenv("CONFIG_PATH", "app_config.json")).strip() or "app_config.json"
    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return normalize_config_data({})
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return normalize_config_data(raw if isinstance(raw, dict) else {})
