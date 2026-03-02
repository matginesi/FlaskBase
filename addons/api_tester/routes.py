from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, current_app, jsonify, render_template, g
from flask_login import current_user, login_required

from app.extensions import db
from app.models import ApiToken, ApiTokenReveal, now_utc
from app.services.access_control import addon_enabled, can_access_addon
from app.services.api_runtime import resolve_api_public_base_url
from app.services.audit import audit

bp = Blueprint(
    "api_tester",
    __name__,
    template_folder="templates",
    url_prefix="/addons/api_tester",
)


@bp.before_request
def _force_addon_english():
    g.ui_lang = "en"

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
    rows: list[dict[str, object]] = [
        {"path": "/v1/health", "methods": ["GET"], "auth_mode": "public", "summary": "API health"},
        {"path": "/v1/meta", "methods": ["GET"], "auth_mode": "public", "summary": "API metadata"},
        {"path": "/v1/meta/routes", "methods": ["GET"], "auth_mode": "bearer", "summary": "Published API routes"},
        {"path": "/v1/auth/me", "methods": ["GET"], "auth_mode": "bearer", "summary": "Current API principal"},
        {"path": "/v1/auth/token", "methods": ["GET"], "auth_mode": "bearer", "summary": "Current token metadata"},
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
        if str(token.name or "").strip().lower() in {"browser-session", "api_tester"}:
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


def _consume_pending_api_tester_reveal() -> str:
    secret_key = str(current_app.config.get("SECRET_KEY", ""))
    row = (
        ApiTokenReveal.query.filter_by(user_id=int(current_user.id), name="api_tester", revealed_at=None)
        .order_by(ApiTokenReveal.created_at.desc())
        .first()
    )
    if row is None:
        return ""
    raw = row.decrypt_raw(secret_key) or ""
    row.revealed_at = now_utc()
    db.session.commit()
    return raw


def _revoke_active_api_tester_tokens() -> None:
    now = now_utc()
    rows = (
        ApiToken.query.filter_by(user_id=int(current_user.id), name="api_tester")
        .filter(ApiToken.revoked_at.is_(None))
        .all()
    )
    changed = False
    for row in rows:
        row.revoked_at = now
        changed = True
    if changed:
        db.session.flush()


def _mint_api_tester_token() -> tuple[ApiToken, str]:
    _revoke_active_api_tester_tokens()
    token, raw = ApiToken.create(
        user_id=int(current_user.id),
        name="api_tester",
        addon_key=None,
        created_by_user_id=int(current_user.id),
        scopes=_build_scope_list(),
        expires_at=now_utc() + timedelta(hours=8),
    )
    db.session.add(token)
    db.session.flush()
    reveal = ApiTokenReveal.create_encrypted(
        user_id=int(current_user.id),
        raw_token=raw,
        token_prefix=token.token_prefix,
        secret_key=str(current_app.config.get("SECRET_KEY", "")),
        name="api_tester",
        expires_at=token.expires_at,
        created_by_user_id=int(current_user.id),
        token_id=int(token.id),
    )
    db.session.add(reveal)
    db.session.commit()
    return token, raw

def _status_payload() -> dict[str, object]:
    return {
        "api_public_base_url": _api_public_base_url(),
        "minted_scopes": _build_scope_list(),
        "timeout_sec": _timeout_sec(),
        "max_response_kb": int(_max_response_bytes() / 1024),
    }


@bp.get("/")
@login_required
def index():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        abort(403)
    audit("page.view", "Viewed API Tester (addon)")
    return render_template(
        "addons/api_tester/index.html",
        view_mode="user",
        api_catalog=_api_catalog(),
        user_tokens=_current_user_tokens(),
        api_status=_status_payload(),
        pretty_json=_pretty_json(),
        latest_tester_token=_consume_pending_api_tester_reveal(),
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
    audit("page.view", "Viewed API Tester admin (addon)")
    return render_template(
        "addons/api_tester/index.html",
        view_mode="admin",
        api_catalog=_api_catalog(),
        user_tokens=_current_user_tokens(),
        api_status=_status_payload(),
        pretty_json=_pretty_json(),
        latest_tester_token=_consume_pending_api_tester_reveal(),
        user_url="/addons/api_tester/",
        admin_url="/addons/api_tester/admin",
    )


@bp.post("/api/mint-token")
@login_required
def mint_token():
    if not addon_enabled("api_tester") or not can_access_addon("api_tester", current_user):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    token, raw = _mint_api_tester_token()
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
