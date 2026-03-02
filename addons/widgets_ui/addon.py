from __future__ import annotations

from flask import Flask

from app.services.addon_contract import AddonManifest, AddonNavItem

from .routes import bp


def build_addon(app: Flask) -> AddonManifest:
    return AddonManifest(
        addon_id="widgets_ui",
        name="Widgets & UI",
        version="1.0.0",
        description="Demo di componenti UI e widget.",
        blueprints=[bp],
        nav_items=[
            AddonNavItem(
                id="widgets",
                label="Widget & UI",
                href="/widgets",
                icon="grid-3x3-gap",
                role="user",
                section="tools",
                order=30,
                page_key="widgets",
                active_prefix="/widgets",
            ),
            AddonNavItem(
                id="widgets_admin",
                label="Widget Admin",
                href="/admin/addons/widgets_ui",
                icon="layout-sidebar-inset",
                role="admin",
                section="tools",
                order=30,
                active_prefix="/admin/addons/widgets_ui",
            ),
        ],
    )
