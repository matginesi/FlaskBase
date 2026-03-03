# Add-ons

## Purpose

Add-ons are the standard way to extend the platform without rebuilding:

- the UI shell
- authentication
- admin flows
- runtime settings
- logging and audit
- database bootstrap

## Minimum Structure

Each built-in add-on lives under `addons/<addon_id>/` and should include at least:

- `__init__.py`
- `addon.py`
- `routes.py`
- `config.json`
- `visual.json`
- `templates/`
- `templates/addons/<addon_id>/`

Typical installable add-ons also include:

- `api.py` when they expose FastAPI routes
- `static/` when they ship local CSS, JS, or images
- `README.md` if the add-on is meant to be packaged and shared

## User and Admin Views

Product rule:

- normal users see user pages
- admins can access both user pages and admin pages

That split should be explicit in routes and templates.

## Manifest

The entry point is `build_addon(app)` in `addon.py`, returning an `AddonManifest`.

It defines:

- `addon_id`
- name
- version
- `description`
- `min_app_version`
- blueprints
- `nav_items`
- `page_endpoint_map`
- `api_mounts`
- `metadata`

Minimal example:

```python
from flask import Flask

from app.services.addon_contract import AddonManifest, AddonNavItem

from .routes import bp


def build_addon(app: Flask) -> AddonManifest:
    return AddonManifest(
        addon_id="my_demo",
        name="My Demo",
        version="1.0.0",
        description="Demo add-on",
        min_app_version="1.0.0",
        blueprints=[bp],
        nav_items=[
            AddonNavItem(
                id="my_demo",
                label="My Demo",
                href="/addons/my_demo/",
                icon="puzzle",
                role="user",
                section="tools",
                order=60,
                page_key="my_demo",
                active_prefix="/addons/my_demo",
            )
        ],
    )
```

## Runtime

The loader:

- discovers add-ons
- validates the manifest
- checks `min_app_version`
- registers navigation
- mounts declared FastAPI routers
- syncs the DB registry
- writes load events

Main file:

- [addon_loader.py](/home/matteo/PycharmProjects/webApp/app/services/addon_loader.py)

## Add-on Persistence

The platform already provides shared tables for:

- `addon_registry`
- `addon_install_events`
- `addon_configs`
- `addon_secrets`
- `addon_grants`
- `addon_data_objects`

See [ADDON_FUNCTIONS.md](/home/matteo/PycharmProjects/webApp/docs/ADDON_FUNCTIONS.md) for details.

## ZIP Installation

Add-ons can be installed from ZIP packages.

The installer validates:

- single root folder
- safe paths
- no symlinks
- presence of `addon.py`, `config.json`, `visual.json`

Activation behavior:

- install copies the add-on into the configured add-on root
- runtime settings are updated to enable it
- a restart is required for new Flask routes to become available
- add-on API mounts are rebuilt together with the runtime add-on state

This is expected: Flask does not safely register brand-new blueprints after the application has already served requests.

## Practical Rules

1. Use platform services before adding new custom tables.
2. Keep the add-on UI aligned with `base.html`.
3. Log real errors and lifecycle events with [app_logger.py](/home/matteo/PycharmProjects/webApp/app/services/app_logger.py) and keep audit entries for explicit user/admin actions.
4. If an add-on has an admin view, the user view must still be usable by admins.
5. Avoid duplicating configuration across files, DB, and code.
6. Treat `config.json` and `visual.json` as defaults and seed values; the runtime source of truth is the database-backed settings layer.
7. Put user-facing navigation items in the main shell, but keep add-on admin view switching inside the add-on page itself.
8. Prefer fenced Markdown code blocks with an explicit language in add-on docs and READMEs:

```json
{
  "enabled": true,
  "welcome_copy": "Hello from My Demo"
}
```

```python
def build_addon(app: Flask) -> AddonManifest:
    ...
```

```bash
python cli.py serve-api
```

## Frontend Safety

Add-on templates must remain compatible with the application CSP:

- no inline `style=` attributes
- no inline `onclick`, `oninput`, or similar handlers
- prefer classes, `data-*` hooks, and nonce-backed scripts/styles when a demo or widget needs local behavior

The `Widget & UI` add-on is the reference for CSP-safe interactive examples.
