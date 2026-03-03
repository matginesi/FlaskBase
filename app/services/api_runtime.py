from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from flask import Flask

from .access_control import can_access_addon_api
from .api_auth import extract_bearer_value, token_has_scopes, validate_api_token


@dataclass
class ApiPrincipal:
    user_id: int
    email: str
    name: str
    role: str
    token_id: int
    token_name: str
    token_prefix: str
    token_addon_key: str | None
    scopes: list[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_api_settings(flask_app: Flask) -> dict[str, Any]:
    cfg = flask_app.config.get("API", {}) or {}
    if not isinstance(cfg, dict):
        return {}
    return dict(cfg)


def resolve_api_public_base_url(flask_app: Flask) -> str:
    cfg = get_api_settings(flask_app)
    base_url = str(flask_app.config.get("BASE_URL", "http://127.0.0.1:5000") or "").strip().rstrip("/")
    default_url = f"{base_url}/api" if base_url else "/api"
    explicit = str(cfg.get("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not explicit:
        return default_url
    return explicit


def build_api_error(*, code: str, detail: str, request_id: str | None = None, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": code,
        "detail": detail,
    }
    if request_id:
        payload["request_id"] = request_id
    if isinstance(extras, dict) and extras:
        payload.update(extras)
    return payload


def get_request_api_principal(request: Any) -> ApiPrincipal | None:
    return getattr(getattr(request, "state", object()), "api_principal", None)


def _principal_from_token(token_row: Any) -> ApiPrincipal:
    user = getattr(token_row, "user", None)
    return ApiPrincipal(
        user_id=int(getattr(user, "id")),
        email=str(getattr(user, "email", "") or ""),
        name=str(getattr(user, "name", "") or ""),
        role=str(getattr(user, "role", "user") or "user"),
        token_id=int(getattr(token_row, "id")),
        token_name=str(getattr(token_row, "name", "") or ""),
        token_prefix=str(getattr(token_row, "token_prefix", "") or ""),
        token_addon_key=(str(getattr(token_row, "addon_key", "")).strip() or None),
        scopes=[str(item).strip() for item in list(getattr(token_row, "scopes_json", []) or []) if str(item).strip()],
    )


def build_api_guard(
    *,
    required_scopes: Sequence[str] | None = None,
    addon_key: str | None = None,
    roles: Sequence[str] | None = None,
):
    async def guard(request: Any, authorization: str | None = None) -> ApiPrincipal:
        from fastapi import Header, HTTPException

        flask_app = getattr(request.app.state, "flask_app", None)
        if flask_app is None:
            raise HTTPException(status_code=500, detail=build_api_error(code="server_error", detail="Flask app context not attached"))

        request_id = str(getattr(request.state, "request_id", "") or "")
        token_value = extract_bearer_value(authorization or "")
        if not token_value:
            raise HTTPException(
                status_code=401,
                detail=build_api_error(code="missing_token", detail="Missing Bearer token", request_id=request_id),
            )

        with flask_app.app_context():
            token_row = validate_api_token(
                token_value,
                required_scopes=list(required_scopes or []),
                addon_key=addon_key,
            )
            if token_row is None:
                raise HTTPException(
                    status_code=401,
                    detail=build_api_error(code="invalid_token", detail="Invalid, expired, or unauthorized token", request_id=request_id),
                )
            principal = _principal_from_token(token_row)
            if addon_key and not can_access_addon_api(addon_key, token_row.user, app=flask_app):
                raise HTTPException(
                    status_code=403,
                    detail=build_api_error(code="addon_forbidden", detail=f"API access denied for add-on '{addon_key}'", request_id=request_id),
                )
            role_set = {str(item).strip().lower() for item in list(roles or ("user", "admin")) if str(item).strip()}
            if role_set and principal.role.lower() not in role_set:
                raise HTTPException(
                    status_code=403,
                    detail=build_api_error(code="role_forbidden", detail="User role is not allowed for this API", request_id=request_id),
                )
            if required_scopes and not token_has_scopes(principal.scopes, required_scopes):
                raise HTTPException(
                    status_code=403,
                    detail=build_api_error(code="missing_scope", detail="Token scope is not sufficient for this API", request_id=request_id),
                )

        request.state.api_principal = principal
        return principal

    from fastapi import Depends, Header, Request

    async def dependency(request: Request, authorization: str | None = Header(default=None)) -> ApiPrincipal:
        return await guard(request=request, authorization=authorization)

    return Depends(dependency)


def principal_to_dict(principal: ApiPrincipal | None) -> dict[str, Any] | None:
    if principal is None:
        return None
    return {
        "user_id": int(principal.user_id),
        "email": principal.email,
        "name": principal.name,
        "role": principal.role,
        "token_id": int(principal.token_id),
        "token_name": principal.token_name,
        "token_prefix": principal.token_prefix,
        "token_addon_key": principal.token_addon_key,
        "scopes": list(principal.scopes),
    }
