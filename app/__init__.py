from __future__ import annotations

import os
import secrets
import time
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any

import ipaddress

from flask import Flask, Response, current_app, g, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import csrf, db, limiter, login_manager, migrate
from .logging_setup import configure_logging
from .services.access_control import addon_access_map
from .services.app_logger import instrument_app_views, install_runtime_logging_hooks, log_exception as emit_exception, log_warning
from .services.app_settings_service import get_effective_settings
from .services.config_service import normalize_theme_settings, normalize_visual_settings
from .services.error_log import log_exception
from .services.message_delivery_service import unread_message_counts_for_user
from .services.pages_service import get_runtime_feature_flags, read_pages
from .services.job_service import init_job_runtime
from .services.i18n import SUPPORTED_LANGUAGES, language_label, resolve_language, translate
from .services.html_sanitize import safe_markup, sanitize_html
from .services.runtime_control import read_runtime_control
from .services.addon_loader import addons_root as resolved_addons_root

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        out = int(str(value).strip())
    except Exception:
        out = int(default)
    if min_value is not None:
        out = max(min_value, out)
    if max_value is not None:
        out = min(max_value, out)
    return out


def _mask_db_uri(uri: str) -> str:
    raw = (uri or "").strip()
    if not raw:
        return ""
    if "@" not in raw or "://" not in raw:
        return raw
    prefix, rest = raw.split("://", 1)
    creds, suffix = rest.split("@", 1)
    if ":" not in creds:
        return raw
    user = creds.split(":", 1)[0]
    return f"{prefix}://{user}:***@{suffix}"


def _safe_request_id(raw: str | None) -> str:
    clean = "".join(ch for ch in str(raw or "") if ch.isalnum() or ch in {"-", "_", "."})
    return clean[:64] if clean else secrets.token_hex(8)


def _parse_csv(value: Any) -> list[str]:
    """Parse a CSV-ish value that might come from DB JSON.

    Accepts:
    - "a,b,c"
    - ["a", "b"]
    - None
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            s = str(item or "").strip().lower()
            if s:
                parts.append(s)
        return parts
    return [part.strip().lower() for part in str(value or "").split(",") if part.strip()]


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Host allow-list checker.

    Supported patterns:
    - "*" (allow all)  [dev only]
    - exact host: "example.com", "localhost", "192.168.1.10"
    - wildcard subdomain: "*.example.com"
    - CIDR: "192.168.1.0/24" (matches if the request host is an IP in that subnet)
    """
    if not allowed_hosts:
        return True

    normalized = (host or "").strip().lower()
    host_only = normalized.split(":", 1)[0] if normalized and not normalized.startswith("[") else normalized
    host_only = host_only.strip("[]")  # tolerate IPv6 like [::1]

    if "*" in allowed_hosts:
        return True

    if host_only in allowed_hosts:
        return True

    # Try CIDR match (only if host is an IP literal)
    try:
        host_ip = ipaddress.ip_address(host_only)
    except Exception:
        host_ip = None
    if host_ip is not None:
        for pattern in allowed_hosts:
            if "/" in pattern:
                try:
                    net = ipaddress.ip_network(pattern, strict=False)
                except Exception:
                    continue
                if host_ip in net:
                    return True

    # Wildcard subdomain
    for pattern in allowed_hosts:
        if pattern.startswith("*.") and host_only.endswith(pattern[1:]):
            return True

    return False


def _derive_production_allowed_hosts(base_url: str) -> list[str]:
    """Derive a safe-but-usable default allow-list from BASE_URL."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(base_url or "").strip())
        host = (parsed.hostname or "").strip().lower()
        return [host] if host else []
    except Exception:
        return []


def _database_uri() -> str:
    uri = str(os.getenv("DATABASE_URL", "")).strip()
    if not uri:
        raise RuntimeError("DATABASE_URL is required and must point to PostgreSQL.")
    if not uri.startswith(("postgresql://", "postgresql+", "postgres://")):
        raise RuntimeError("SQLite is no longer supported. Configure PostgreSQL in DATABASE_URL.")
    return uri


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    if test_config:
        app.config.update(test_config)

    cfg, theme_cfg, visual_cfg = get_effective_settings()
    settings_cfg = dict(cfg.get("SETTINGS", {}) or {})
    sec_cfg = dict(settings_cfg.get("SECURITY", {}) or {})
    addons_cfg = dict(cfg.get("ADDONS", {}) or {})

    env_name = str(os.getenv("APP_ENV", os.getenv("FLASK_ENV", os.getenv("ENV", "development")))).strip().lower()
    is_production = env_name in {"prod", "production"}

    secret_key = str(os.getenv("SECRET_KEY", "")).strip()
    if not secret_key:
        if is_production:
            raise RuntimeError("SECRET_KEY is required in production.")
        secret_key = secrets.token_hex(32)

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=app.config.get("SQLALCHEMY_DATABASE_URI") or _database_uri(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_as_bool(os.getenv("SESSION_COOKIE_SECURE"), is_production),
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=_as_bool(os.getenv("REMEMBER_COOKIE_SECURE"), is_production),
        REMEMBER_COOKIE_DURATION=timedelta(
            days=_as_int(sec_cfg.get("REMEMBER_COOKIE_DAYS", os.getenv("REMEMBER_COOKIE_DAYS", 14)), 14, min_value=1, max_value=365)
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(
            minutes=_as_int(sec_cfg.get("SESSION_TIMEOUT_MIN", 120), 120, min_value=5, max_value=1440)
        ),
        WTF_CSRF_ENABLED=_as_bool(sec_cfg.get("CSRF_ENABLED", True), True),
        WTF_CSRF_TIME_LIMIT=_as_int(sec_cfg.get("CSRF_TIME_LIMIT_SEC", 3600), 3600, min_value=60, max_value=86400),
        MAX_CONTENT_LENGTH=_as_int(sec_cfg.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024), 16 * 1024 * 1024, min_value=1024),
        APP_CONFIG_EFFECTIVE=cfg,
        SETTINGS=settings_cfg,
        SECURITY=sec_cfg,
        LOGGING=dict(settings_cfg.get("LOGGING") or {}),
        AUTH=dict(settings_cfg.get("AUTH") or {}),
        EMAIL=dict(settings_cfg.get("EMAIL") or {}),
        API=dict(settings_cfg.get("API") or {}),
        DASHBOARD=dict(settings_cfg.get("DASHBOARD") or {}),
        THEME=theme_cfg,
        VISUAL=visual_cfg,
        ADDONS=addons_cfg,
        ADDON_POLICIES=dict(addons_cfg.get("ITEMS") or {}),
        ADDON_SETTINGS=dict(settings_cfg.get("ADDONS_CONFIG") or {}),
        APP_NAME=str(settings_cfg.get("APP_NAME", "WebApp")).strip() or "WebApp",
        APP_VERSION=str(settings_cfg.get("APP_VERSION", "1.0.0")).strip() or "1.0.0",
        BASE_URL=str(settings_cfg.get("BASE_URL", "http://127.0.0.1:5000")).strip() or "http://127.0.0.1:5000",
        SEED_PATH=str(os.getenv("SEED_PATH", "seed/seed.json")).strip() or "seed/seed.json",
        ADDONS_ROOT=str(Path(os.getenv("ADDONS_ROOT", "")).resolve()) if str(os.getenv("ADDONS_ROOT", "")).strip() else str(resolved_addons_root()),
    )

    proxy_x_for = _as_int(os.getenv("PROXY_FIX_X_FOR", 0), 0, min_value=0, max_value=5)
    proxy_x_proto = _as_int(os.getenv("PROXY_FIX_X_PROTO", 0), 0, min_value=0, max_value=5)
    proxy_x_host = _as_int(os.getenv("PROXY_FIX_X_HOST", 0), 0, min_value=0, max_value=5)
    proxy_x_port = _as_int(os.getenv("PROXY_FIX_X_PORT", 0), 0, min_value=0, max_value=5)
    if any(v > 0 for v in (proxy_x_for, proxy_x_proto, proxy_x_host, proxy_x_port)):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_x_for, x_proto=proxy_x_proto, x_host=proxy_x_host, x_port=proxy_x_port)

    os.makedirs(app.instance_path, exist_ok=True)

    configure_logging(app)
    install_runtime_logging_hooks()
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.session_protection = "strong"
    csrf.init_app(app)

    # Sync app.config with DB-backed runtime settings as soon as we have an app context.
    # This matters for decorators (e.g. rate limits) that read from current_app.config
    # and would otherwise stick to the JSON seed loaded before db.init_app().
    try:
        with app.app_context():
            from .services.app_settings_service import get_app_settings_raw, _apply_runtime_to_app

            row = get_app_settings_raw()
            _apply_runtime_to_app(
                config=dict(row.config_json or {}),
                theme=dict(row.theme_json or {}),
                visual=dict(row.visual_json or {}),
                row=row,
            )
    except Exception:
        # DB may not be ready yet (e.g. first bootstrap). Seed config remains in place.
        pass

    memory_storage_uri = "memory" + "://"
    storage_uri = str(os.getenv("RATELIMIT_STORAGE_URI", memory_storage_uri)).strip() or memory_storage_uri

    # In tests we prefer determinism over security controls.
    if bool(app.config.get("TESTING")):
        app.config["RATELIMIT_ENABLED"] = False
    if storage_uri == memory_storage_uri and is_production:
        app.logger.error(
            "RATELIMIT_STORAGE_URI must be set to a shared storage in production. "
            "In-memory rate limiting is ineffective with multiple workers."
        )
        raise RuntimeError(
            "RATELIMIT_STORAGE_URI must be set to a shared storage (Redis/Memcached) in production. "
            "In-memory rate limiting is ineffective with multiple workers."
        )
    if storage_uri == memory_storage_uri:
        warnings.warn("Rate limiter uses in-memory storage and is not shared across workers.", RuntimeWarning)
        app.logger.warning("Rate limiter uses in-memory storage and is not shared across workers.")
    app.config["RATELIMIT_STORAGE_URI"] = storage_uri
    limiter.init_app(app)

    from .blueprints.admin.routes import bp as admin_bp
    from .blueprints.auth.routes import bp as auth_bp
    from .blueprints.main.routes import bp as main_bp
    from .services.addon_loader import load_addons, sync_addon_runtime_state
    from .services.fastapi_bridge import proxy_fastapi_request

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)

    @app.route("/api", defaults={"api_path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @app.route("/api/<path:api_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def fastapi_proxy(api_path: str = ""):
        return proxy_fastapi_request(api_path)

    app.extensions["page_endpoint_map"] = {
        "main.dashboard": ("user", "dashboard"),
        "main.messages": ("user", "messages"),
        "auth.settings": ("user", "account_settings"),
        "main.privacy": ("user", "privacy"),
        "admin.dashboard": ("admin", "admin_dashboard"),
        "admin.settings": ("admin", "admin_settings"),
        "admin.users": ("admin", "admin_users"),
        "admin.logs": ("admin", "admin_logs"),
        "admin.database": ("admin", "admin_database"),
        "admin.deploy_health": ("admin", "admin_deploy_health"),
    }
    app.extensions.setdefault("addon_nav", {"user": [], "admin": []})
    app.extensions.setdefault("addon_registry", {})
    with app.app_context():
        load_addons(app)
    instrument_app_views(app)

    @app.get("/favicon.ico")
    def favicon():
        return redirect(url_for("static", filename="favicon.svg"), code=302)

    @app.before_request
    def inject_runtime_state():
        from flask_login import current_user, logout_user

        g.request_started_at = time.time()
        g.request_id = _safe_request_id(request.headers.get("X-Request-ID"))
        g.csp_nonce = secrets.token_urlsafe(24)
        runtime_cfg, runtime_theme, runtime_visual = get_effective_settings()
        runtime_settings = dict(runtime_cfg.get("SETTINGS", {}) or {})
        runtime_sec_cfg = dict(runtime_settings.get("SECURITY", {}) or {})
        runtime_api_cfg = dict(runtime_settings.get("API", {}) or {})
        runtime_addons_cfg = dict(runtime_cfg.get("ADDONS", {}) or {})
        runtime_addon_policies = dict(runtime_addons_cfg.get("ITEMS") or {}) if isinstance(runtime_addons_cfg.get("ITEMS"), dict) else {}
        runtime_addon_settings = dict(runtime_settings.get("ADDONS_CONFIG", {}) or {})
        g.runtime_config = {
            "APP_NAME": str(runtime_settings.get("APP_NAME", app.config.get("APP_NAME", "WebApp"))).strip() or "WebApp",
            "APP_VERSION": str(runtime_settings.get("APP_VERSION", app.config.get("APP_VERSION", "1.0.0"))).strip() or "1.0.0",
            "BASE_URL": str(runtime_settings.get("BASE_URL", app.config.get("BASE_URL", "http://127.0.0.1:5000"))).strip() or "http://127.0.0.1:5000",
            "THEME": runtime_theme,
            "VISUAL": runtime_visual,
            "SETTINGS": runtime_settings,
            "SECURITY": runtime_sec_cfg,
            "API": runtime_api_cfg,
            "ADDONS": runtime_addons_cfg,
            "ADDON_POLICIES": runtime_addon_policies,
            "ADDON_SETTINGS": runtime_addon_settings,
        }
        g.app_name = g.runtime_config["APP_NAME"]
        g.app_version = g.runtime_config["APP_VERSION"]
        user_locale = getattr(current_user, "locale", None) if getattr(current_user, "is_authenticated", False) else None
        g.ui_lang_default = str(runtime_settings.get("UI_LANGUAGE", "en")).strip().lower()
        g.ui_lang = resolve_language(user_locale=user_locale, default_language=g.ui_lang_default)
        g.ui_lang_label = language_label(g.ui_lang)
        g.supported_languages = dict(SUPPORTED_LANGUAGES)
        g.theme = normalize_theme_settings(runtime_theme)
        g.visual = normalize_visual_settings(runtime_visual)
        g.pages = read_pages()
        g.runtime_features = get_runtime_feature_flags()
        g.features = {}
        try:
            sync_addon_runtime_state(app)
        except Exception as exc:
            log_warning(
                "addons.runtime_sync_failed",
                "Failed to synchronize in-memory add-on state before request handling",
                context={"error": str(exc)[:240]},
            )
        g.addons = app.extensions.get("addons", {})
        g.addon_nav = app.extensions.get("addon_nav", {"user": [], "admin": []})
        g.addon_registry = app.extensions.get("addon_registry", {})
        g.addon_access = addon_access_map(current_user)
        g.cookie_banner_enabled = bool(g.runtime_features.get("cookie_banner", True))
        runtime_control = read_runtime_control(app)
        g.runtime_refresh_token = str(runtime_control.get("refresh_token", "")).strip()
        g.runtime_refresh_message = str(runtime_control.get("refresh_message", "")).strip()
        g.message_counts = {"unread_total": 0, "unread_user": 0, "unread_broadcast": 0}
        if getattr(current_user, "is_authenticated", False):
            try:
                g.message_counts = unread_message_counts_for_user(getattr(current_user, "id", None))
            except Exception:
                g.message_counts = {"unread_total": 0, "unread_user": 0, "unread_broadcast": 0}

        # Host allow-list
        # - Dev: default allow-all (practical for LAN testing)
        # - Prod: default derived from BASE_URL, with explicit overrides
        default_local = ["localhost", "127.0.0.1", "[::1]", "::1"]
        allowed_hosts = _parse_csv(runtime_sec_cfg.get("ALLOWED_HOSTS"))

        if not is_production:
            dev_allow_all = _as_bool(runtime_sec_cfg.get("DEV_ALLOW_ALL_HOSTS", os.getenv("DEV_ALLOW_ALL_HOSTS", "1")), True)
            if dev_allow_all:
                allowed_hosts = ["*"]
            elif not allowed_hosts:
                # Still usable without being totally open: allow local + private subnets.
                allowed_hosts = default_local + ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]
        else:
            # Never allow '*' in production.
            allowed_hosts = [h for h in allowed_hosts if h != "*"]

            # If user didn't configure anything (or left dev defaults), derive from BASE_URL.
            if (not allowed_hosts) or set(allowed_hosts).issubset(set(default_local)):
                allowed_hosts = _derive_production_allowed_hosts(g.runtime_config.get("BASE_URL") or app.config.get("BASE_URL") or "")

            # Optional: extend via environment variable (useful for docker/proxy setups)
            extra_hosts = _parse_csv(os.getenv("EXTRA_ALLOWED_HOSTS"))
            for h in extra_hosts:
                if h and h not in allowed_hosts:
                    allowed_hosts.append(h)


        # Expose deployment-related runtime info to templates and admin diagnostics.
        g.is_production = bool(is_production)
        g.proxy_fix = {"x_for": proxy_x_for, "x_proto": proxy_x_proto, "x_host": proxy_x_host, "x_port": proxy_x_port}
        g.allowed_hosts_effective = list(allowed_hosts or [])
        g.request_host = str(request.host or "")
        g.request_host_allowed = bool(_host_allowed(request.host, allowed_hosts)) if allowed_hosts else True
        g.forwarded = {
            "x_forwarded_for": str(request.headers.get("X-Forwarded-For", "") or "")[:200],
            "x_forwarded_proto": str(request.headers.get("X-Forwarded-Proto", "") or "")[:64],
            "x_forwarded_host": str(request.headers.get("X-Forwarded-Host", "") or "")[:128],
            "x_forwarded_port": str(request.headers.get("X-Forwarded-Port", "") or "")[:16],
        }

        # Human-friendly deployment warnings (shown to admins).
        warnings_list: list[dict[str, str]] = []
        base_url = str(g.runtime_config.get("BASE_URL") or "").strip()

        def _add_warn(level: str, title: str, detail: str) -> None:
            warnings_list.append({"level": level, "title": title, "detail": detail})

        if is_production:
            # BASE_URL should be set to the canonical public URL in production.
            try:
                from urllib.parse import urlparse

                parsed = urlparse(base_url)
                bu_host = (parsed.hostname or "").strip().lower()
                bu_scheme = (parsed.scheme or "").strip().lower()
            except Exception:
                bu_host, bu_scheme = "", ""

            if not bu_host:
                _add_warn("danger", "BASE_URL missing", "Set SETTINGS.BASE_URL to your public URL (e.g. https://example.com).")
            elif bu_host in {"127.0.0.1", "localhost"}:
                _add_warn("warning", "BASE_URL is local", "BASE_URL points to localhost; external links and host checks may break.")
            if bu_scheme and bu_scheme != "https":
                _add_warn("warning", "BASE_URL not HTTPS", "Use https:// in production to ensure secure cookies and correct URL generation.")

            # Proxy headers present but ProxyFix disabled → request.host/proto may be wrong.
            if any(g.forwarded.get(k) for k in ("x_forwarded_proto", "x_forwarded_host")) and not any(
                v > 0 for v in (proxy_x_for, proxy_x_proto, proxy_x_host, proxy_x_port)
            ):
                _add_warn(
                    "warning",
                    "Reverse proxy headers detected",
                    "ProxyFix is disabled but X-Forwarded-* headers are present. Enable PROXY_FIX_* in .env if running behind nginx/traefik.",
                )
        else:
            # Dev ergonomics hint.
            if "*" in (allowed_hosts or []):
                _add_warn("info", "Dev host allow-all enabled", "DEV_ALLOW_ALL_HOSTS is enabled (good for LAN testing).")

        g.deployment_warnings = warnings_list
        if not warnings_list:
            g.deployment_status = "ok"
        elif any(w.get("level") == "danger" for w in warnings_list):
            g.deployment_status = "danger"
        else:
            g.deployment_status = "warn"

        if allowed_hosts and not g.request_host_allowed:
            return render_template("errors/403.html"), 403

        if current_user.is_authenticated:
            timeout_seconds = int(app.permanent_session_lifetime.total_seconds())
            now_ts = int(time.time())
            last_seen = _as_int(session.get("_last_activity_ts", now_ts), now_ts, min_value=0)
            if (now_ts - last_seen) >= timeout_seconds:
                logout_user()
                session.clear()
                return redirect(url_for("auth.login"))
            session["_last_activity_ts"] = now_ts
            session.modified = True
            try:
                from .models import UserSession, now_utc

                browser_session_id = session.get("browser_session_id")
                if browser_session_id:
                    browser_session = db.session.get(UserSession, int(browser_session_id))
                    if browser_session and browser_session.is_valid():
                        current_seen = getattr(browser_session, "last_seen_at", None)
                        now_seen = now_utc()
                        if current_seen is None or (now_seen - current_seen).total_seconds() >= 30:
                            browser_session.last_seen_at = now_seen
                            db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            session.pop("_last_activity_ts", None)

        endpoint_map = app.extensions.get("page_endpoint_map", {})
        mapped = endpoint_map.get(request.endpoint or "")
        if mapped:
            group, key = mapped
            enabled = bool(g.pages.get("pages", {}).get(group, {}).get(key, {}).get("enabled", True))
            if not enabled:
                return render_template("errors/404.html"), 404

    @app.context_processor
    def inject_i18n_helpers():
        return {
            "tr": translate,
            "supported_languages": dict(SUPPORTED_LANGUAGES),
        }

    @app.after_request
    def add_security_headers(response):
        nonce = getattr(g, "csp_nonce", "")
        nonce_part = f" 'nonce-{nonce}'" if nonce else ""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'; "
            "connect-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https:; "
            f"style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com{nonce_part}; "
            f"style-src-elem 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com{nonce_part}; "
            "style-src-attr 'none'; "
            f"script-src 'self' https://cdn.jsdelivr.net{nonce_part}; "
            f"script-src-elem 'self' https://cdn.jsdelivr.net{nonce_part}; "
            "script-src-attr 'none'; "
            "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com;"
        )
        if is_production or request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )
        if request.path.startswith(("/admin/", "/auth/", "/api/")):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
            response.headers.setdefault("Expires", "0")
        if getattr(g, "request_id", None):
            response.headers.setdefault("X-Request-ID", g.request_id)

        try:
            duration_ms = int((time.time() - float(getattr(g, "request_started_at", time.time()))) * 1000)
            level = "INFO"
            event_type = "request.completed"
            if response.status_code >= 500:
                level = "ERROR"
                event_type = "request.server_error"
            elif response.status_code >= 400:
                level = "WARNING"
                event_type = "request.client_error"
            getattr(app.logger, level.lower())(
                event_type,
                extra={
                    "event_type": event_type,
                    "context": {
                        "request_id": getattr(g, "request_id", None),
                        "endpoint": request.endpoint,
                        "path": request.path,
                        "method": request.method,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                    },
                },
            )
        except Exception as exc:
            log_warning("request.logging_failed", "Failed to write request completion log", context={"error": str(exc)[:240]})
        return response

    @app.template_filter("tojson_log")
    def tojson_log_filter(event):
        import json

        return json.dumps(
            {
                "ts": str(getattr(event, "ts", "") or ""),
                "level": str(getattr(event, "level", "") or ""),
                "event_type": str(getattr(event, "event_type", "") or ""),
                "message": str(getattr(event, "message", "") or ""),
                "user_email": getattr(getattr(event, "user", None), "email", "") or "",
                "ip": str(getattr(event, "ip", "") or ""),
                "path": str(getattr(event, "path", "") or ""),
                "method": str(getattr(event, "method", "") or ""),
                "context": dict(getattr(event, "context", {}) or {}),
            },
            ensure_ascii=False,
        )

    @app.template_filter("sanitize_html_fragment")
    def sanitize_html_fragment_filter(value: Any):
        return safe_markup(sanitize_html(str(value or "")))

    @app.get("/")
    def index():
        return redirect(url_for("main.dashboard"))

    @app.errorhandler(403)
    def forbidden(_: Exception):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_: Exception):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def payload_too_large(_: Exception):
        return render_template("errors/413.html"), 413

    @app.errorhandler(429)
    def too_many_requests(_: Exception):
        return render_template("errors/429.html"), 429

    @app.errorhandler(Exception)
    def handle_error(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        try:
            if not getattr(exc, "_app_logged", False):
                log_exception(exc, ctx={"request_id": getattr(g, "request_id", None), "endpoint": request.endpoint})
        except Exception as log_exc:
            emit_exception(log_exc, event_type="request.error_logging_failed", logger=app.logger)
        return render_template("errors/500.html"), 500

    app.logger.info(
        "app.ready",
        extra={
            "event_type": "app.ready",
            "context": {
                "app_name": app.config["APP_NAME"],
                "app_version": app.config["APP_VERSION"],
                "database_url": _mask_db_uri(app.config["SQLALCHEMY_DATABASE_URI"]),
            },
        },
    )
    return app
