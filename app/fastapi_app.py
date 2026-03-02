from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware


def create_fastapi_app(flask_app, root_path: str = ""):
    from app.services.api_runtime import (
        build_api_error,
        build_api_guard,
        get_api_settings,
        get_request_api_principal,
        principal_to_dict,
        resolve_api_public_base_url,
    )

    api_cfg = _api_config(flask_app)
    docs_enabled = bool(api_cfg.get("DOCS_ENABLED", True))
    openapi_enabled = bool(api_cfg.get("OPENAPI_ENABLED", True))
    redoc_enabled = bool(api_cfg.get("REDOC_ENABLED", False))

    app = FastAPI(
        title=f"{str(flask_app.config.get('APP_NAME', 'WebApp')).strip() or 'WebApp'} API",
        version=str(flask_app.config.get("APP_VERSION", "1.0.0")).strip() or "1.0.0",
        root_path=str(root_path or "").strip(),
        docs_url=str(api_cfg.get("DOCS_PATH", "/docs")) if docs_enabled else None,
        openapi_url=str(api_cfg.get("OPENAPI_PATH", "/openapi.json")) if openapi_enabled else None,
        redoc_url=str(api_cfg.get("REDOC_PATH", "/redoc")) if redoc_enabled else None,
        swagger_ui_parameters={"displayRequestDuration": True, "defaultModelsExpandDepth": -1},
    )
    app.state.flask_app = flask_app
    app.state.api_public_base_url = resolve_api_public_base_url(flask_app)

    origins = [item for item in _split_csv(api_cfg.get("CORS_ALLOWED_ORIGINS", "")) if item]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            allow_credentials=False,
            max_age=600,
        )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        request.state.request_id = _safe_request_id(request.headers.get("X-Request-ID"))
        host_error = _host_validation_error(flask_app, request)
        if host_error is not None:
            return JSONResponse(status_code=400, content=host_error)
        size_error = _request_size_error(flask_app, request)
        if size_error is not None:
            return JSONResponse(status_code=413, content=size_error)
        with flask_app.app_context():
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else build_api_error(
            code=f"http_{int(exc.status_code)}",
            detail=str(exc.detail or "HTTP error"),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=int(exc.status_code), content=detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=build_api_error(
                code="validation_error",
                detail="Request validation failed",
                request_id=getattr(request.state, "request_id", None),
                extras={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=build_api_error(
                code="server_error",
                detail="Unexpected server error",
                request_id=getattr(request.state, "request_id", None),
            ),
        )

    core = APIRouter(prefix="/v1", tags=["core"])

    @core.get("/health", summary="API health")
    async def api_health(request: Request) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "api",
            "app": str(flask_app.config.get("APP_NAME", "WebApp")),
            "version": str(flask_app.config.get("APP_VERSION", "1.0.0")),
            "request_id": getattr(request.state, "request_id", None),
        }

    @core.get("/meta", summary="API metadata", dependencies=[build_api_guard(roles=("user", "admin"))])
    async def api_meta(request: Request) -> dict[str, Any]:
        return {
            "ok": True,
            "app": str(flask_app.config.get("APP_NAME", "WebApp")),
            "version": str(flask_app.config.get("APP_VERSION", "1.0.0")),
            "api_public_base_url": app.state.api_public_base_url,
            "docs_enabled": docs_enabled,
            "openapi_enabled": openapi_enabled,
            "addon_count": len(flask_app.extensions.get("addon_api_mounts", {}) or {}),
            "request_id": getattr(request.state, "request_id", None),
        }

    @core.get(
        "/meta/routes",
        summary="Published API routes",
        dependencies=[build_api_guard(required_scopes=("profile:read",), roles=("user", "admin"))],
    )
    async def api_routes(request: Request) -> dict[str, Any]:
        return {
            "ok": True,
            "items": _route_catalog(app),
            "request_id": getattr(request.state, "request_id", None),
        }

    @core.get(
        "/auth/me",
        summary="Current API principal",
        dependencies=[build_api_guard(roles=("user", "admin"))],
    )
    async def api_me(request: Request) -> dict[str, Any]:
        return {
            "ok": True,
            "principal": principal_to_dict(get_request_api_principal(request)),
            "request_id": getattr(request.state, "request_id", None),
        }

    @core.get(
        "/auth/token",
        summary="Current token metadata",
        dependencies=[build_api_guard(roles=("user", "admin"))],
    )
    async def api_token(request: Request) -> dict[str, Any]:
        principal = get_request_api_principal(request)
        return {
            "ok": True,
            "token": None if principal is None else {
                "id": int(principal.token_id),
                "name": principal.token_name,
                "prefix": principal.token_prefix,
                "addon_key": principal.token_addon_key,
                "scopes": list(principal.scopes),
            },
            "request_id": getattr(request.state, "request_id", None),
        }

    app.include_router(core)
    _include_addon_routers(app, flask_app)

    return app


def _api_config(flask_app) -> dict[str, Any]:
    from app.services.api_runtime import get_api_settings

    env_name = str(os.getenv("APP_ENV", os.getenv("FLASK_ENV", os.getenv("ENV", "development")))).strip().lower()
    is_production = env_name in {"prod", "production"}
    defaults = {
        "PUBLIC_BASE_URL": "",
        "DOCS_ENABLED": not is_production,
        "OPENAPI_ENABLED": not is_production,
        "REDOC_ENABLED": False,
        "DOCS_PATH": "/docs",
        "OPENAPI_PATH": "/openapi.json",
        "REDOC_PATH": "/redoc",
        "CORS_ALLOWED_ORIGINS": "",
    }
    cfg = get_api_settings(flask_app)
    merged = dict(defaults)
    merged.update({key: value for key, value in cfg.items() if value is not None})
    return merged


def _split_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _safe_request_id(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw and len(raw) <= 80 and all(ch.isalnum() or ch in {"-", "_", "."} for ch in raw):
        return raw
    return "api_" + secrets.token_urlsafe(12)


def _strip_port(host: str) -> str:
    raw = str(host or "").strip()
    if raw.startswith("[") and "]" in raw:
        return raw[1:raw.index("]")]
    if ":" in raw:
        return raw.split(":", 1)[0]
    return raw


def _host_validation_error(flask_app, request):
    from app.services.api_runtime import build_api_error

    allowed = [item.lower() for item in _split_csv((flask_app.config.get("SECURITY", {}) or {}).get("ALLOWED_HOSTS", ""))]
    if not allowed:
        return None
    host = _strip_port(request.headers.get("host", "")).lower()
    if host and host in {item.lower().strip("[]") for item in allowed}:
        return None
    return build_api_error(
        code="invalid_host",
        detail="Host header is not allowed for this API",
        request_id=getattr(request.state, "request_id", None),
    )


def _request_size_error(flask_app, request):
    from app.services.api_runtime import build_api_error

    max_len = int((flask_app.config.get("SECURITY", {}) or {}).get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024) or 16 * 1024 * 1024)
    try:
        length = int(request.headers.get("content-length", "0") or "0")
    except Exception:
        length = 0
    if length > max_len:
        return build_api_error(
            code="payload_too_large",
            detail="Payload exceeds configured API size limit",
            request_id=getattr(request.state, "request_id", None),
        )
    return None


def _include_addon_routers(api_app, flask_app) -> None:
    from app.services.api_runtime import build_api_guard

    addon_mounts = flask_app.extensions.get("addon_api_mounts", {}) or {}
    mounted: list[dict[str, Any]] = []
    for addon_id, mounts in dict(addon_mounts).items():
        for mount in list(mounts or []):
            router = mount.build_router(flask_app)
            dependencies = []
            if not bool(mount.public):
                dependencies.append(
                    build_api_guard(
                        required_scopes=tuple(mount.required_scopes or ()),
                        addon_key=str(addon_id),
                        roles=tuple(mount.roles or ("user", "admin")),
                    )
                )
            api_app.include_router(
                router,
                prefix=str(mount.prefix),
                tags=list(mount.tags or [str(addon_id)]),
                dependencies=dependencies,
                include_in_schema=bool(mount.include_in_schema),
            )
            mounted.append(
                {
                    "addon_id": str(addon_id),
                    "id": str(mount.id),
                    "prefix": str(mount.prefix),
                    "public": bool(mount.public),
                    "roles": list(mount.roles or ()),
                    "required_scopes": list(mount.required_scopes or ()),
                    "summary": str(mount.summary or ""),
                }
            )
    api_app.state.addon_api_mounts = mounted


def _route_catalog(api_app) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base_url = str(getattr(api_app.state, "api_public_base_url", "") or "").rstrip("/")
    for route in list(api_app.routes):
        path = str(getattr(route, "path", "") or "")
        methods = sorted(method for method in list(getattr(route, "methods", set()) or set()) if method not in {"HEAD", "OPTIONS"})
        if not path or not methods:
            continue
        if not path.startswith("/v1/") and path not in {"/docs", "/openapi.json", "/redoc"}:
            continue
        rows.append(
            {
                "path": path,
                "url": f"{base_url}{path}" if base_url else path,
                "methods": methods,
                "name": str(getattr(route, "name", "") or ""),
            }
        )
    rows.sort(key=lambda item: (item["path"], ",".join(item["methods"])))
    return rows
