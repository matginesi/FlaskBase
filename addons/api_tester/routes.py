from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlparse
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import ApiToken, now_utc
from app.services.access_control import addon_enabled, can_access_addon
from app.services.api_auth import validate_api_token
from app.services.app_logger import get_logger, log_error, log_event, log_warning
from app.services.api_runtime import resolve_api_public_base_url
from app.services.audit import audit

bp = Blueprint(
    "api_tester",
    __name__,
    template_folder="templates",
    url_prefix="/addons/api_tester",
)

_ALLOWED_PROXY_PATHS = ("/v1/", "/docs", "/openapi.json", "/redoc")
log = get_logger(__name__)


def _settings() -> dict[str, object]:
    cfg = current_app.config.get("ADDON_SETTINGS", {}) or {}
    if not isinstance(cfg, dict):
        return {}
    row = cfg.get("api_tester", {}) if isinstance(cfg.get("api_tester", {}), dict) else {}
    return dict(row)


def _timeout_sec() -> int:
    try:
        return max(1, min(120, int(_settings().get("timeout_sec", 30) or 30)))
    except Exception:
        return 30


def _max_response_bytes() -> int:
    try:
        kb = int(_settings().get("max_response_kb", 512) or 512)
    except Exception:
        kb = 512
    return max(10, min(10240, kb)) * 1024


def _pretty_json() -> bool:
    return bool(_settings().get("pretty_json", True))


def _api_public_base_url() -> str:
    return resolve_api_public_base_url(current_app)


def _api_start_command() -> str:
    return "No separate API process is required. Use the main WebApp server and keep APIs enabled in Config WebApp."


def _meta_payload() -> dict[str, str]:
    user = getattr(current_user, "email", None) or getattr(current_user, "username", None) or str(getattr(current_user, "id", ""))
    return {
        "addon": "api_tester",
        "user": str(user),
        "role": str(getattr(current_user, "role", "") or ""),
    }


def _build_scope_list() -> list[str]:
    scopes = {"profile:read", "api_tester:*"}
    addon_mounts = current_app.extensions.get("addon_api_mounts", {}) or {}
    for addon_id in dict(addon_mounts).keys():
        clean = str(addon_id or "").strip()
        if clean:
            scopes.add(f"{clean}:read")
            if getattr(current_user, "is_admin", lambda: False)():
                scopes.add(f"{clean}:*")
    if getattr(current_user, "is_admin", lambda: False)():
        scopes.add("admin")
    return sorted(scopes)


def _api_catalog() -> list[dict[str, object]]:
    base_url = _api_public_base_url()
    rows: list[dict[str, object]] = [
        {"path": "/v1/health", "url": f"{base_url}/v1/health" if base_url else "/v1/health", "methods": ["GET"], "auth_mode": "public", "summary": "API health"},
        {"path": "/v1/meta", "url": f"{base_url}/v1/meta" if base_url else "/v1/meta", "methods": ["GET"], "auth_mode": "public", "summary": "API metadata"},
        {"path": "/v1/meta/routes", "url": f"{base_url}/v1/meta/routes" if base_url else "/v1/meta/routes", "methods": ["GET"], "auth_mode": "bearer", "summary": "Published API routes"},
        {"path": "/v1/auth/me", "url": f"{base_url}/v1/auth/me" if base_url else "/v1/auth/me", "methods": ["GET"], "auth_mode": "bearer", "summary": "Current API principal"},
        {"path": "/v1/auth/token", "url": f"{base_url}/v1/auth/token" if base_url else "/v1/auth/token", "methods": ["GET"], "auth_mode": "bearer", "summary": "Current token metadata"},
    ]
    addon_mounts = current_app.extensions.get("addon_api_mounts", {}) or {}
    for addon_id, mounts in dict(addon_mounts).items():
        for mount in list(mounts or []):
            prefix = str(getattr(mount, "prefix", "") or "").strip()
            if not prefix:
                continue
            rows.append(
                {
                    "path": prefix,
                    "url": f"{base_url}{prefix}" if base_url else prefix,
                    "methods": ["GET"],
                    "auth_mode": "public" if bool(getattr(mount, "public", False)) else "bearer",
                    "summary": str(getattr(mount, "summary", "") or f"{addon_id} add-on API"),
                }
            )
    rows.sort(key=lambda item: str(item.get("path", "")))
    return rows


def _current_user_tokens() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for token in ApiToken.query.filter_by(user_id=int(current_user.id)).order_by(ApiToken.created_at.desc()).limit(12).all():
        if not token.is_valid():
            continue
        rows.append(
            {
                "id": int(token.id),
                "name": str(token.name or "default"),
                "prefix": str(token.token_prefix or ""),
                "addon_key": str(token.addon_key or ""),
                "expires_at": token.expires_at.isoformat() if token.expires_at else None,
                "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
                "scopes": list(token.scopes_json or []),
                "restricted": bool(token.addon_key),
            }
        )
    return rows


def _mint_api_tester_token() -> tuple[ApiToken, str]:
    token, raw = ApiToken.create(
        user_id=int(current_user.id),
        name="api_tester",
        addon_key=None,
        created_by_user_id=int(current_user.id),
        scopes=_build_scope_list(),
        expires_at=now_utc() + timedelta(hours=8),
    )
    db.session.add(token)
    db.session.commit()
    return token, raw


def _validate_path(path: str) -> str | None:
    clean = "/" + str(path or "").strip().lstrip("/")
    for prefix in _ALLOWED_PROXY_PATHS:
        if clean == prefix or clean.startswith(prefix):
            return clean
    return None


def _proxy_api_request(*, path: str, method: str, raw_token: str | None = None, payload: str | None = None) -> tuple[int, dict[str, object]]:
    base_url = _api_public_base_url()
    if not base_url:
        log_warning(
            "addon.api_tester.proxy_missing_base_url",
            "API Tester proxy requested without configured public base URL",
            logger=log,
            context={"path": path, "method": method},
        )
        return 503, {"ok": False, "error": "api_public_base_url_missing"}
    safe_path = _validate_path(path)
    if safe_path is None:
        log_warning(
            "addon.api_tester.path_not_allowed",
            "API Tester rejected disallowed proxy path",
            logger=log,
            context={"path": path, "method": method, "user_id": int(current_user.id)},
        )
        return 400, {"ok": False, "error": "path_not_allowed", "allowed_prefixes": list(_ALLOWED_PROXY_PATHS)}
    url = f"{base_url}{safe_path}"
    body = None if method == "GET" else (payload or "{}").encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if raw_token:
        headers["Authorization"] = f"Bearer {raw_token}"
    req = urlrequest.Request(url, data=body, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=_timeout_sec()) as resp:
            raw = resp.read(_max_response_bytes() + 1)
            if len(raw) > _max_response_bytes():
                log_warning(
                    "addon.api_tester.response_too_large",
                    "API Tester received a response larger than the configured cap",
                    logger=log,
                    context={"path": safe_path, "method": method, "limit_kb": int(_max_response_bytes() / 1024)},
                )
                return 502, {"ok": False, "error": "response_too_large", "limit_kb": int(_max_response_bytes() / 1024)}
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed: object = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"raw": text}
            return int(resp.status), {"ok": True, "status": int(resp.status), "url": url, "payload": parsed}
    except urlerror.HTTPError as ex:
        raw = ex.read(_max_response_bytes() + 1)
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"raw": text}
        log_warning(
            "addon.api_tester.proxy_http_error",
            "API Tester upstream request returned an HTTP error",
            logger=log,
            context={"path": safe_path, "method": method, "status": int(ex.code), "user_id": int(current_user.id)},
        )
        return int(ex.code), {"ok": False, "status": int(ex.code), "url": url, "payload": parsed}
    except Exception as ex:
        log_error(
            "addon.api_tester.proxy_failed",
            "API Tester upstream request failed",
            logger=log,
            context={"path": safe_path, "method": method, "user_id": int(current_user.id)},
            exc_info=(type(ex), ex, ex.__traceback__),
        )
        return 502, {"ok": False, "error": "proxy_failed", "detail": str(ex)[:240], "url": url}


def _status_payload() -> dict[str, object]:
    base_url = _api_public_base_url()
    status_code, result = _proxy_api_request(path="/v1/health", method="GET")
    detail = str(result.get("detail", "") or "").strip()
    return {
        "api_public_base_url": base_url,
        "api_configured": bool(base_url),
        "api_reachable": bool(status_code < 500 and result.get("ok")),
        "health_status": status_code,
        "health_result": result,
        "health_detail": detail,
        "minted_scopes": _build_scope_list(),
        "timeout_sec": _timeout_sec(),
        "max_response_kb": int(_max_response_bytes() / 1024),
        "start_command": _api_start_command(),
    }


@bp.get("/")
@login_required
def index():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        abort(403)
    log_event(
        "INFO",
        "addon.api_tester.page_view",
        "Rendered API Tester user page",
        logger=log,
        context={"user_id": int(current_user.id), "role": str(getattr(current_user, "role", "") or "user")},
    )
    audit("page.view", "Viewed API Tester (addon)")
    return render_template(
        "addons/api_tester/index.html",
        view_mode="user",
        api_catalog=_api_catalog(),
        user_tokens=_current_user_tokens(),
        api_public_base_url=_api_public_base_url(),
        api_status=_status_payload(),
        pretty_json=_pretty_json(),
        user_url="/addons/api_tester/",
        admin_url="/addons/api_tester/admin",
    )


@bp.get("/admin")
@login_required
def admin():
    if not addon_enabled("api_tester"):
        abort(404)
    if not getattr(current_user, "is_admin", lambda: False)():
        abort(403)
    log_event(
        "INFO",
        "addon.api_tester.admin_page_view",
        "Rendered API Tester admin page",
        logger=log,
        context={"user_id": int(current_user.id), "role": str(getattr(current_user, "role", "") or "")},
    )
    audit("page.view", "Viewed API Tester admin (addon)")
    return render_template(
        "addons/api_tester/index.html",
        view_mode="admin",
        api_catalog=_api_catalog(),
        user_tokens=_current_user_tokens(),
        api_public_base_url=_api_public_base_url(),
        api_status=_status_payload(),
        pretty_json=_pretty_json(),
        user_url="/addons/api_tester/",
        admin_url="/addons/api_tester/admin",
    )


@bp.get("/api/status")
@login_required
def api_status():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, **_status_payload(), **_meta_payload()})


@bp.post("/api/mint-token")
@login_required
def mint_token():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    token, raw = _mint_api_tester_token()
    log_event(
        "INFO",
        "addon.api_tester.token_minted",
        "Minted temporary API Tester token",
        logger=log,
        context={"user_id": int(current_user.id), "token_id": int(token.id), "token_prefix": str(token.token_prefix or "")},
    )
    audit("addon.api_tester_token_minted", "Minted API Tester token", context={"uid": int(current_user.id), "token_id": int(token.id)})
    return jsonify(
        {
            "ok": True,
            "token": raw,
            "token_id": int(token.id),
            "token_prefix": token.token_prefix,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "scopes": list(token.scopes_json or []),
            "tokens": _current_user_tokens(),
        }
    )


@bp.get("/api/catalog")
@login_required
def api_catalog():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "items": _api_catalog(), "tokens": _current_user_tokens(), "api_public_base_url": _api_public_base_url(), **_meta_payload()})


@bp.post("/api/run")
@login_required
def run_request():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    path = _validate_path(data.get("path", "/v1/health"))
    if path is None:
        log_warning(
            "addon.api_tester.path_not_allowed",
            "API Tester rejected request for unsupported path",
            logger=log,
            context={"user_id": int(current_user.id), "path": str(data.get("path", "")), "method": str(data.get("method", "GET")).upper()},
        )
        return jsonify({"ok": False, "error": "path_not_allowed", "allowed_prefixes": list(_ALLOWED_PROXY_PATHS)}), 400
    method = str(data.get("method", "GET")).strip().upper()
    if method not in {"GET", "POST"}:
        log_warning(
            "addon.api_tester.unsupported_method",
            "API Tester rejected unsupported HTTP method",
            logger=log,
            context={"user_id": int(current_user.id), "path": path, "method": method},
        )
        return jsonify({"ok": False, "error": "unsupported_method"}), 400
    raw_token = str(data.get("token", "") or "").strip()
    if raw_token:
        token_row = validate_api_token(raw_token)
        if token_row is None:
            log_warning(
                "addon.api_tester.invalid_token",
                "API Tester rejected invalid bearer token",
                logger=log,
                context={"user_id": int(current_user.id), "path": path, "method": method},
            )
            return jsonify({"ok": False, "error": "invalid_token"}), 401
        if int(token_row.user_id) != int(current_user.id):
            log_warning(
                "addon.api_tester.token_not_owned",
                "API Tester rejected bearer token owned by another user",
                logger=log,
                context={"user_id": int(current_user.id), "token_owner_id": int(token_row.user_id), "path": path, "method": method},
            )
            return jsonify({"ok": False, "error": "token_not_owned_by_current_user"}), 403
    payload = data.get("payload")
    payload_text = payload if isinstance(payload, str) else json.dumps(payload or {})
    status, result = _proxy_api_request(path=path, method=method, raw_token=raw_token or None, payload=payload_text)
    log_event(
        "INFO" if status < 400 else "WARNING",
        "addon.api_tester.request_completed",
        "API Tester proxy request completed",
        logger=log,
        context={"user_id": int(current_user.id), "path": path, "method": method, "status": status, "token_supplied": bool(raw_token)},
    )
    return jsonify({"ok": status < 400, "proxy_status": status, "result": result, **_meta_payload()}), status
