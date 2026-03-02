# Codebase

## Purpose

This document is a quick map of the current project structure without relying on stale or legacy assumptions.

## Application Core

- [app/__init__.py](/home/matteo/PycharmProjects/webApp/app/__init__.py)
  Flask factory, base security, blueprint registration, add-on loading, request lifecycle.
- [app/extensions.py](/home/matteo/PycharmProjects/webApp/app/extensions.py)
  Shared Flask/SQLAlchemy/Login/CSRF/Limiter extensions.
- [app/models.py](/home/matteo/PycharmProjects/webApp/app/models.py)
  Application and platform database schema.
- [app/logging_setup.py](/home/matteo/PycharmProjects/webApp/app/logging_setup.py)
  Console, file, and DB logging setup.

## Blueprints

- [auth](/home/matteo/PycharmProjects/webApp/app/blueprints/auth)
  Login, session handling, MFA, tokens, user settings.
- [main](/home/matteo/PycharmProjects/webApp/app/blueprints/main)
  Dashboard, messages, privacy, core user pages.
- [admin](/home/matteo/PycharmProjects/webApp/app/blueprints/admin)
  Runtime settings, users, logs, database, jobs, add-ons.

## Platform Services

- [app/fastapi_app.py](/home/matteo/PycharmProjects/webApp/app/fastapi_app.py)
  External FastAPI application, core API routes, middleware, and add-on API mounting.
- [app_settings_service.py](/home/matteo/PycharmProjects/webApp/app/services/app_settings_service.py)
  DB-backed runtime settings, robust form parsing, runtime apply, and JSON import/export payloads.
- [seed_service.py](/home/matteo/PycharmProjects/webApp/app/services/seed_service.py)
  Initial user and platform seed.
- [addon_loader.py](/home/matteo/PycharmProjects/webApp/app/services/addon_loader.py)
  Add-on discovery, loading, navigation, add-on API mount registration, and import-cache invalidation for reinstall/update flows.
- [addon_installer.py](/home/matteo/PycharmProjects/webApp/app/services/addon_installer.py)
  ZIP install/export/uninstall.
- [addon_data_service.py](/home/matteo/PycharmProjects/webApp/app/services/addon_data_service.py)
  Registry, config, secrets, storage, grants, lifecycle.
- [audit.py](/home/matteo/PycharmProjects/webApp/app/services/audit.py)
  Persistent audit events.
- [app_logger.py](/home/matteo/PycharmProjects/webApp/app/services/app_logger.py)
  Runtime warning/exception logging.
- [api_runtime.py](/home/matteo/PycharmProjects/webApp/app/services/api_runtime.py)
  Shared FastAPI principal resolution, request guards, and API error payloads.
- [api_auth.py](/home/matteo/PycharmProjects/webApp/app/services/api_auth.py)
  Shared bearer-token validation and scope checks.
- [job_service.py](/home/matteo/PycharmProjects/webApp/app/services/job_service.py)
  Background job runtime, queues, worker loop, and async delivery handlers.
- [database_admin_service.py](/home/matteo/PycharmProjects/webApp/app/services/database_admin_service.py)
  Database overview, read-only SQL console logic, maintenance actions, and snapshot export.

## Database

The project is PostgreSQL-only.

Main tables:

- `users`
- `user_sessions`
- `api_tokens`
- `log_events`
- `app_settings`
- `addon_registry`
- `addon_install_events`
- `addon_configs`
- `addon_secrets`
- `addon_grants`
- `addon_data_objects`
- `job_queues`
- `job_runs`
- `broadcast_messages`
- `user_messages`

Local bootstrap:

- [init_postgres.sh](/home/matteo/PycharmProjects/webApp/init_postgres.sh)

Admin DB management:

- [app/templates/admin/database.html](/home/matteo/PycharmProjects/webApp/app/templates/admin/database.html)
  PostgreSQL overview page with maintenance and diagnostics.
- [app/templates/admin/settings.html](/home/matteo/PycharmProjects/webApp/app/templates/admin/settings.html)
  Full runtime settings UI for app, security, auth, email, logging, theme, visual settings, JSON import/export, and add-ons.

Seed data:

- [seed/seed.json](/home/matteo/PycharmProjects/webApp/seed/seed.json)

## Configuration

- [.env](/home/matteo/PycharmProjects/webApp/.env)
  DB URLs, secrets, deployment flags.
- [app_config.json](/home/matteo/PycharmProjects/webApp/app_config.json)
  Seed-only app defaults, including API runtime defaults and initial page/feature definitions.
- `app_settings`
  Runtime source of truth for app identity, core settings, add-ons, pages, theme, visual settings, revision, and import/export metadata.

Runtime update flow:

1. admin form payload is normalized in `app_settings_service`
2. settings row is updated and committed in PostgreSQL
3. the saved row is refreshed
4. Flask runtime config is rebuilt from the persisted row
5. add-ons are reloaded on top of the new runtime state

This is important because the source of truth is the database row, not the raw submitted form.

## Frontend

- [app/templates](/home/matteo/PycharmProjects/webApp/app/templates)
  Flask templates.
- [app/static/css/app.css](/home/matteo/PycharmProjects/webApp/app/static/css/app.css)
  Design tokens, shell styles, shared components.
- [app/static/js](/home/matteo/PycharmProjects/webApp/app/static/js)
  App bootstrap, sidebar/topbar behavior, modals, toasts, feature scripts.
- [ui_template](/home/matteo/PycharmProjects/webApp/ui_template)
  Static copy of the real application UI.

## Add-ons

- [addons](/home/matteo/PycharmProjects/webApp/addons)
  Built-in add-ons loaded with Flask views and optional FastAPI mounts.
- [addon_packages](/home/matteo/PycharmProjects/webApp/addon_packages)
  Installable/exportable add-on packages and sources.

Activation rule:

- changing add-on enable/disable flags is a runtime config change and is applied from `app_settings`
- installing a ZIP add-on copies files and updates runtime config, but new Flask routes still require a restart to become available
- uninstall removes the add-on folder and prunes runtime config for that add-on

## Tests

- [tests/conftest.py](/home/matteo/PycharmProjects/webApp/tests/conftest.py)
  Pytest fixtures.
- [tests/test_all.py](/home/matteo/PycharmProjects/webApp/tests/test_all.py)
  End-to-end stress runner.

## What Is Not the Runtime Source of Truth

- `ui_template/` is a static visual copy, not backend logic
- `app_config.json` is seed-only and not the final runtime settings store
- seed files do not replace runtime tables
