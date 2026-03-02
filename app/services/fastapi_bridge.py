from __future__ import annotations

import asyncio

from flask import Response, current_app, request


_DROP_REQUEST_HEADERS = {"host", "content-length", "connection"}
_DROP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}


def proxy_fastapi_request(api_path: str = "") -> Response:
    import httpx

    from app.fastapi_app import create_fastapi_app

    flask_app = current_app._get_current_object()
    api_cfg = flask_app.config.get("API", {}) or {}
    if isinstance(api_cfg, dict) and not bool(api_cfg.get("ENABLED", True)):
        return Response("API disabled", status=404)

    target_path = "/" + str(api_path or "").lstrip("/")
    if target_path == "//":
        target_path = "/"
    if request.query_string:
        target_path = f"{target_path}?{request.query_string.decode('utf-8', errors='ignore')}"

    forwarded_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    forwarded_headers["Host"] = request.host
    forwarded_headers.setdefault("X-Forwarded-Proto", request.scheme)
    forwarded_headers.setdefault("X-Forwarded-Host", request.host)

    body = request.get_data(cache=True, as_text=False)
    api_app = create_fastapi_app(flask_app, root_path="/api")

    async def _dispatch() -> httpx.Response:
        transport = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://fastapi.local") as client:
            return await client.request(
                method=request.method,
                url=target_path,
                content=body if body else None,
                headers=forwarded_headers,
            )

    api_response = asyncio.run(_dispatch())

    response_headers = [
        (key, value)
        for key, value in api_response.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    ]
    return Response(
        api_response.content,
        status=int(api_response.status_code),
        headers=response_headers,
    )
