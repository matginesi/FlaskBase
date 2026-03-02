from __future__ import annotations

from datetime import datetime, timedelta
from functools import wraps
import re
from typing import Callable, Iterable, Optional, TypeVar, cast

from flask import current_app, g, has_app_context, request
try:
    from flask_smorest import abort  # type: ignore
except Exception:  # pragma: no cover
    from flask import abort  # type: ignore

from ..extensions import db
from ..models import ApiToken, User, now_utc

F = TypeVar("F", bound=Callable[..., object])
TOKEN_RE = re.compile(r"^sfk_[A-Za-z0-9_\-]{20,200}$")


def extract_bearer_value(authorization: str | None) -> Optional[str]:
    auth = str(authorization or "").strip()
    if len(auth) > 4096:
        return None
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    return token or None


def _extract_bearer_token() -> Optional[str]:
    auth = extract_bearer_value(request.headers.get("Authorization"))
    if auth:
        return auth
    alt = (request.headers.get("X-API-Token") or "").strip()
    if len(alt) > 512:
        return None
    return alt or None


def extract_api_token() -> Optional[str]:
    return _extract_bearer_token()


def _normalize_scopes(raw_scopes: Iterable[str] | None) -> set[str]:
    out: set[str] = set()
    for item in list(raw_scopes or []):
        raw = str(item or "").strip().lower()
        if raw:
            out.add(raw)
    return out


def token_has_scopes(token_scopes: Iterable[str] | None, required_scopes: Iterable[str] | None) -> bool:
    required = _normalize_scopes(required_scopes)
    if not required:
        return True
    granted = _normalize_scopes(token_scopes)
    if "*" in granted:
        return True
    for need in required:
        if need in granted:
            continue
        matched = False
        for scope in granted:
            if scope.endswith(":*") and need.startswith(scope[:-1]):
                matched = True
                break
        if not matched:
            return False
    return True


def validate_api_token(raw_token: str, *, required_scopes: Iterable[str] | None = None, addon_key: str | None = None) -> Optional[ApiToken]:
    token = (raw_token or "").strip()
    if not token or not TOKEN_RE.match(token):
        return None
    hashed = ApiToken.hash_token(token)
    tok = ApiToken.query.filter_by(token_hash=hashed).first()
    if not tok or not tok.is_valid():
        return None
    usr = db.session.get(User, int(tok.user_id))
    if not usr or not usr.is_active:
        return None
    if addon_key:
        wanted_addon = str(addon_key).strip().lower()
        token_addon = str(tok.addon_key or "").strip().lower()
        if token_addon and token_addon != wanted_addon:
            return None
    if required_scopes and not token_has_scopes(tok.scopes_json or [], required_scopes):
        return None
    now = now_utc()
    last = tok.last_used_at
    touch_sec = 30
    if has_app_context():
        sec = dict(current_app.config.get("SECURITY", {}) or {})
        try:
            touch_sec = max(1, int(sec.get("API_TOKEN_TOUCH_INTERVAL_SEC", 30)))
        except Exception:
            touch_sec = 30
    if last is None or (now - last) >= timedelta(seconds=touch_sec):
        tok.last_used_at = now
        db.session.commit()
    return tok


def api_token_required(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        raw = _extract_bearer_token()
        tok = validate_api_token(raw or "")
        if tok is None:
            abort(401, message="Token API mancante o non valido")
        g.api_token_id = tok.id
        g.api_user = tok.user
        return fn(*args, **kwargs)

    return cast(F, wrapper)
