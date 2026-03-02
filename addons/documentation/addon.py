from __future__ import annotations

from flask import Flask

from app.services.addon_contract import AddonApiMount, AddonManifest, AddonNavItem

from .api import build_router as build_api_router
from .routes import bp


def build_addon(app: Flask) -> AddonManifest:
    # NOTE: Keep addon_id stable for upgrade-in-place.
    return AddonManifest(
        addon_id="documentation",
        name="Documentation",
        version="2.0.1",
        description="Viewer della documentazione Markdown (docs/ + README.md) con TOC e Mermaid.",
        blueprints=[bp],
        api_mounts=[
            AddonApiMount(
                id="documentation_api",
                build_router=build_api_router,
                prefix="/v1/addons/documentation",
                tags=["addons", "documentation"],
                required_scopes=("profile:read",),
                summary="Documentation add-on API",
            ),
        ],
        nav_items=[
            AddonNavItem(
                id="docs",
                label="Documentation",
                href="/addons/documentation/",
                icon="file-earmark-text",
                role="user",
                section="tools",
                order=40,
                page_key="docs_viewer",
                active_prefix="/addons/documentation",
            ),
            AddonNavItem(
                id="docs_admin",
                label="Documentation Admin",
                href="/admin/addons/documentation",
                icon="journal-richtext",
                role="admin",
                section="tools",
                order=40,
                active_prefix="/admin/addons/documentation",
            ),
        ],
    )
