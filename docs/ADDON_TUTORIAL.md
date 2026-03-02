# Create an Add-on

## Goal

This tutorial creates a minimal but correct add-on with:

- a user view
- an admin view
- integrated navigation
- runtime config
- logging and audit
- ZIP installer compatibility

## 1. Create the Structure

```text
addons/my_demo/
  addon.py
  routes.py
  api.py
  config.json
  visual.json
  templates/
    addons/
      my_demo/
        index.html
        admin.html
```

## 2. Write `config.json`

`config.json` contains add-on defaults.

```json
{
  "enabled": true,
  "welcome_copy": "Hello from My Demo"
}
```

## 3. Write `visual.json`

```json
{
  "title": "My Demo",
  "icon": "puzzle",
  "description": "Example add-on"
}
```

## 4. Write `routes.py`

```python
from __future__ import annotations

from flask import Blueprint, abort, render_template
from flask_login import current_user, login_required

from app.services.access_control import can_access_addon
from app.services.audit import audit

bp = Blueprint(
    "my_demo",
    __name__,
    template_folder="templates",
    url_prefix="/addons/my_demo",
)


@bp.get("/")
@login_required
def index():
    if not can_access_addon("my_demo", current_user):
        abort(403)
    audit("addon.my_demo.view_user", "Viewed My Demo user page")
    return render_template("addons/my_demo/index.html")


@bp.get("/admin")
@login_required
def admin():
    if getattr(current_user, "role", "") != "admin":
        abort(403)
    audit("addon.my_demo.view_admin", "Viewed My Demo admin page")
    return render_template("addons/my_demo/admin.html")
```

## 5. Write `addon.py`

```python
from __future__ import annotations

from flask import Flask

from app.services.addon_contract import AddonApiMount, AddonManifest, AddonNavItem

from .api import build_router as build_api_router
from .routes import bp


def build_addon(app: Flask) -> AddonManifest:
    return AddonManifest(
        addon_id="my_demo",
        name="My Demo",
        version="1.0.0",
        description="Demo add-on",
        blueprints=[bp],
        api_mounts=[
            AddonApiMount(
                id="my_demo_api",
                build_router=build_api_router,
                prefix="/v1/addons/my-demo",
                tags=["addons", "my_demo"],
                required_scopes=("profile:read",),
                summary="My Demo add-on API",
            ),
        ],
        nav_items=[
            AddonNavItem(
                id="my_demo",
                label="My Demo",
                href="/addons/my_demo/",
                icon="puzzle",
                role="user",
                order=60,
                active_prefix="/addons/my_demo",
            ),
            AddonNavItem(
                id="my_demo_admin",
                label="My Demo Admin",
                href="/addons/my_demo/admin",
                icon="shield-check",
                role="admin",
                order=60,
                active_prefix="/addons/my_demo/admin",
            ),
        ],
    )
```

## 6. Add `api.py`

```python
from __future__ import annotations

from fastapi import APIRouter, Request

from app.services.api_runtime import get_request_api_principal, principal_to_dict


def build_router(app) -> APIRouter:
    router = APIRouter()

    @router.get("/hello")
    async def hello(request: Request):
        principal = get_request_api_principal(request)
        return {
            "ok": True,
            "addon": "my_demo",
            "principal": principal_to_dict(principal),
        }

    return router
```

## 7. Write the Templates

User view:

```html
{% extends "base.html" %}
{% block title %} · My Demo{% endblock %}
{% block content %}
<div class="page-header">
  <div class="page-breadcrumb">Add-on</div>
  <h1>My Demo</h1>
  <p>User view for the add-on.</p>
</div>
<div class="card">
  <div class="card-body">User content.</div>
</div>
{% endblock %}
```

Admin view:

```html
{% extends "base.html" %}
{% block title %} · My Demo Admin{% endblock %}
{% block content %}
<div class="page-header">
  <div class="page-breadcrumb">Add-on Admin</div>
  <h1>My Demo Admin</h1>
  <p>Administrative view for the add-on.</p>
</div>
<div class="card">
  <div class="card-body">Admin content.</div>
</div>
{% endblock %}
```

## 8. Save Runtime Config

```python
from app.services.addon_data_service import get_addon_config, set_addon_config

cfg = get_addon_config("my_demo", "default") or {}
set_addon_config("my_demo", "default", {"welcome_copy": "New text"})
```

## 9. Save Secrets

```python
from app.services.addon_data_service import get_addon_secret, set_addon_secret

set_addon_secret("my_demo", "api_key", "super-secret-token")
token = get_addon_secret("my_demo", "api_key")
```

## 10. Save Small State or Payloads

```python
from app.services.addon_data_service import upsert_addon_data_object

upsert_addon_data_object(
    "my_demo",
    bucket="state",
    object_key="dashboard",
    json_value={"ok": True},
)
```

## 11. Manual Check

1. start the app
2. sign in as `admin@test.com`
3. open the user view
4. open the admin view
5. start FastAPI with `python cli.py serve-api`
6. call the add-on endpoint with a valid bearer token
7. verify desktop sidebar and mobile top navigation
8. check logs

## 12. Build the ZIP

Valid structure:

```text
my_demo.zip
  my_demo/
    addon.py
    routes.py
    api.py
    config.json
    visual.json
    templates/
      addons/
        my_demo/
          index.html
          admin.html
```

## Practical Rules

1. The user view should be designed for normal users.
2. Admins should be able to access both the user and admin views.
3. Always extend `base.html`.
4. Reuse platform services for config, secrets, and storage.
5. Avoid disconnected JS/CSS patterns.
