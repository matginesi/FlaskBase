from __future__ import annotations

from fastapi import APIRouter, Request

from app.services.api_runtime import get_request_api_principal, principal_to_dict


def build_router(app) -> APIRouter:
    router = APIRouter()

    @router.get("/health", summary="Documentation addon API health")
    async def documentation_health(request: Request):
        principal = get_request_api_principal(request)
        return {
            "ok": True,
            "addon": "documentation",
            "status": "ready",
            "principal": principal_to_dict(principal),
            "request_id": getattr(request.state, "request_id", None),
        }

    return router
