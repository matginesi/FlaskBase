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

- `addon.py`
- `routes.py`
- `config.json`
- `visual.json`
- `templates/`

## User and Admin Views

Product rule:

- normal users see user pages
- admins can access both user pages and admin pages

That split should be explicit in routes and templates.

## Manifest

The entry point is `build_addon(app)` in `addon.py`, returning an `AddonManifest`.

It defines:

- id
- name
- version
- blueprints
- nav items
- page endpoint map

## Runtime

The loader:

- discovers add-ons
- validates the manifest
- registers navigation
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

This is expected: Flask does not safely register brand-new blueprints after the application has already served requests.

## Practical Rules

1. Use platform services before adding new custom tables.
2. Keep the add-on UI aligned with `base.html`.
3. Log real errors and lifecycle events.
4. If an add-on has an admin view, the user view must still be usable by admins.
5. Avoid duplicating configuration across files, DB, and code.
6. Treat `config.json` and `visual.json` as defaults and seed values; the runtime source of truth is the database-backed settings layer.
7. Put user-facing navigation items in the main shell, but keep add-on admin view switching inside the add-on page itself.
