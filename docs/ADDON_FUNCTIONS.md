# Add-on Functions

## Purpose

This document describes the platform APIs add-ons should use before introducing custom tables, secret stores, or lifecycle systems.

Main file:

- [addon_data_service.py](/home/matteo/PycharmProjects/webApp/app/services/addon_data_service.py)

## Registry

Function:

- `ensure_addon_registry(addon_key, ...)`

Use it to:

- create or update the registry row
- store core metadata
- keep version, source, manifest, and status in sync

Typical use cases:

- seed
- ZIP installation
- runtime load
- sync/update flows

## Runtime Config

Functions:

- `set_addon_config(...)`
- `get_addon_config(...)`

Table:

- `addon_configs`

Correct use:

- global add-on config
- per-user add-on config
- simple revision tracking

Do not store secrets here.

## Secrets

Functions:

- `set_addon_secret(...)`
- `get_addon_secret(...)`

Table:

- `addon_secrets`

Correct use:

- third-party API keys
- OAuth tokens
- technical credentials

Values are encrypted using the application secret derived from `SECRET_KEY`.

## Add-on Storage

Functions:

- `upsert_addon_data_object(...)`
- `get_addon_data_object(...)`

Table:

- `addon_data_objects`

Suitable for:

- small to medium state
- JSON cache
- text payloads
- limited binary blobs
- operational metadata

Important fields:

- `bucket`
- `object_key`
- `scope`
- `owner_user_id`
- `checksum_sha256`
- `size_bytes`

## Grants and Capabilities

Function:

- `grant_addon_capability(...)`

Table:

- `addon_grants`

Use it for:

- role-based capabilities
- user-specific capabilities
- add-on specific permissions

Examples:

- `view_user`
- `view_admin`
- `jobs.run`
- `storage.write`
- `secrets.manage`

## Lifecycle Events

Function:

- `record_addon_install_event(...)`

Table:

- `addon_install_events`

Tracks:

- install
- update
- load
- seed
- enable / disable
- uninstall

## Runtime Manifest

Defined in:

- [addon_contract.py](/home/matteo/PycharmProjects/webApp/app/services/addon_contract.py)

Objects:

- `AddonManifest`
- `AddonNavItem`
- `AddonApiMount`

Key fields:

- `addon_id`
- `name`
- `version`
- `blueprints`
- `nav_items`
- `page_endpoint_map`
- `api_mounts`

## FastAPI Add-on APIs

Built-in file:

- [fastapi_app.py](/home/matteo/PycharmProjects/webApp/app/fastapi_app.py)

Shared helpers:

- [api_runtime.py](/home/matteo/PycharmProjects/webApp/app/services/api_runtime.py)
- [api_auth.py](/home/matteo/PycharmProjects/webApp/app/services/api_auth.py)

Use `AddonApiMount` when an add-on needs to publish HTTP APIs through the external FastAPI server.

Important properties:

- `prefix`
- `build_router`
- `public`
- `roles`
- `required_scopes`
- `tags`

Security model:

- add-on APIs are mounted only if the add-on is enabled
- non-public mounts require a valid bearer token
- bearer tokens are validated against `api_tokens`
- optional scope checks are enforced before the router runs
- add-on access policy is checked through `can_access_addon_api(...)`

At request time, add-on endpoints can read the resolved API principal from:

- `get_request_api_principal(request)`

Use this instead of custom token parsing inside the add-on.

## Loader

File:

- [addon_loader.py](/home/matteo/PycharmProjects/webApp/app/services/addon_loader.py)

It:

- discovers add-ons under `addons/`
- reads manifests
- registers blueprints
- builds navigation
- updates runtime registry state
- writes load events

## ZIP Installer

File:

- [addon_installer.py](/home/matteo/PycharmProjects/webApp/app/services/addon_installer.py)

Constraints:

- one root folder inside the ZIP
- safe paths only
- no symlinks
- required files:
  `addon.py`, `config.json`, `visual.json`

## Practical Rules

1. Use platform services first for config, secrets, storage, and lifecycle.
2. Add custom tables only when the data model genuinely requires them.
3. Always log lifecycle changes and real failures.
4. Never store secrets in `config.json` or `addon_configs`.
5. Keep user and admin views clearly separated.
