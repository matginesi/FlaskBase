from __future__ import annotations

from flask import Flask

from app.services.addon_contract import AddonApiMount, AddonManifest, AddonNavItem

from .api import build_router as build_api_router
from .routes import bp


def build_addon(app: Flask) -> AddonManifest:
    return AddonManifest(
        addon_id="hello_world",
        name="Hello World",
        version="1.0.0",
        description="Addon di esempio minimale.",
        blueprints=[bp],
        api_mounts=[
            AddonApiMount(
                id="hello_world_api",
                build_router=build_api_router,
                prefix="/v1/addons/hello-world",
                tags=["addons", "hello_world"],
                required_scopes=("profile:read",),
                summary="Hello World add-on API",
            ),
        ],
        nav_items=[
            AddonNavItem(
                id="hello",
                label="Hello World",
                href="/hello",
                icon="emoji-smile",
                role="user",
                section="tools",
                order=10,
                page_key="hello_world",
                active_prefix="/hello",
            ),
            AddonNavItem(
                id="hello_admin",
                label="Hello Admin",
                href="/admin/addons/hello_world",
                icon="shield-check",
                role="admin",
                section="tools",
                order=10,
                active_prefix="/admin/addons/hello_world",
            ),
        ],
    )
