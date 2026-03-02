from __future__ import annotations

from fastapi import APIRouter, Request

from app.services.api_runtime import get_request_api_principal, principal_to_dict


def build_router(app) -> APIRouter:
    router = APIRouter()

    @router.get("/hello", summary="Hello World addon API")
    async def hello_world_api(request: Request):
        principal = get_request_api_principal(request)
        return {
            "ok": True,
            "addon": "hello_world",
            "message": "Hello from the Hello World add-on API.",
            "principal": principal_to_dict(principal),
            "request_id": getattr(request.state, "request_id", None),
        }

    return router
