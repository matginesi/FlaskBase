from __future__ import annotations

from flask import Flask

from app.services.addon_contract import AddonManifest, AddonNavItem

from .routes import bp


def build_addon(app: Flask) -> AddonManifest:
    return AddonManifest(
        addon_id="api_tester",
        name="API Tester",
        version="2.1.0",
        description="Installable add-on for testing the secure FastAPI surface with user-owned bearer tokens.",
        blueprints=[bp],
        nav_items=[
            AddonNavItem(
                id="api_tester",
                label="API Tester",
                href="/addons/api_tester/",
                icon="plug",
                role="user",
                section="tools",
                order=45,
                page_key="api_tester",
                active_prefix="/addons/api_tester",
            ),
            AddonNavItem(
                id="api_tester_admin",
                label="API Tester Admin",
                href="/addons/api_tester/admin",
                icon="shield-lock",
                role="admin",
                section="tools",
                order=45,
                active_prefix="/addons/api_tester/admin",
            ),
        ],
    )
