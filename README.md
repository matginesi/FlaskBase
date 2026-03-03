# WebApp Base

WebApp Base is a Flask and PostgreSQL application foundation for internal tools, MVPs, and modular business apps.

UI showcase: https://matginesi.github.io/FlaskBase/

It ships with a ready-to-run web UI, authentication, admin operations, runtime configuration stored in the database, a FastAPI integration surface, background job execution, and an add-on system for extending the platform without rebuilding the core project structure.

## Highlights

- Flask web application with PostgreSQL as the primary datastore
- Database-backed runtime settings with import/export support
- Built-in admin area for operations, logs, users, jobs, and database diagnostics
- Authentication with sessions, MFA, email confirmation, and personal API keys
- FastAPI surface for external integrations and token-based access
- Add-on architecture for user pages, admin pages, and API mounts
- Background job runtime with queue management and operational controls
- English and Italian UI support
- Seed-based bootstrap for local development

## Platform Scope

This repository is intended to remove the repetitive setup work common to small products and internal platforms. It provides the core application shell so feature work can be added as first-party code or as installable add-ons.

Typical use cases:

- internal dashboards
- admin portals
- workflow tools
- integration hubs
- customer-facing MVPs with a protected admin backend

## Core Architecture

The project is split into a few clear layers:

- `app/`
  Main Flask application, models, services, blueprints, templates, and static assets.
- `addons/`
  Built-in add-ons loaded by the application at runtime.
- `seed/seed.json`
  Seed data for development bootstrap.
- `app_config.json`
  Initial configuration seed used to create the first runtime settings row.
- `ui_template/`
  Standalone UI reference aligned with the real application shell.

Runtime configuration is persisted in PostgreSQL and applied from the database after startup. The JSON files are seed inputs, not the long-term source of truth.

## Main Capabilities

### Application and Admin

- user and admin dashboards
- runtime settings editor
- user management
- audit and event logs
- database diagnostics and maintenance
- communication and inbox workflows
- job and queue operations

### Authentication and Security

- session-based authentication
- MFA enrollment and challenge flow
- email verification support
- personal API key creation and revocation
- CSRF protection
- host allow-list enforcement
- request size limits
- security-aware logging and audit events

### API Platform

- FastAPI routes under `/v1/*`
- bearer-token authentication backed by `api_tokens`
- token scopes and add-on-aware API policy support
- runtime-configurable docs and OpenAPI exposure
- add-on API mounts

### Extensibility

- built-in add-ons from the repository
- ZIP install and export flow for add-ons
- add-on navigation entries
- add-on API mounts
- add-on-specific runtime settings

## Included Modules

The default repository seed enables example modules such as:

- documentation
- hello world
- widgets UI

The project also includes an API Tester add-on for exercising the exposed API surface from the application UI.

## Technology Stack

- Python
- Flask
- FastAPI
- SQLAlchemy
- PostgreSQL
- Jinja2
- Bootstrap
- Gunicorn for multi-worker web serving

## Quick Start

### 1. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Start from the example file:

```bash
cp .env.example .env
```

Set at minimum:

- `SECRET_KEY`
- `DATABASE_URL`
- `TEST_DATABASE_URL`

### 3. Initialize PostgreSQL

```bash
bash init_postgres.sh
python cli.py init-db-complete --force
```

### 4. Start the web application

```bash
python cli.py serve --host 127.0.0.1 --port 5000
```

### 5. Start the API surface

```bash
python cli.py serve-api --host 127.0.0.1 --port 8000
```

## Multi-Process Local Runtime

For a more realistic local setup:

```bash
python cli.py serve-stack --host 127.0.0.1 --port 5000 --web-workers 2 --threads 4 --job-workers 1
```

This starts:

- Gunicorn web workers for concurrent requests
- a dedicated background job worker process

## Default Seed Accounts

Development seed credentials:

- `admin@test.com` / `admin`
- `user@test.com` / `user`

These are for local bootstrap only and should never be reused outside development.

## Configuration Model

The project uses three configuration layers:

1. `.env`
   Deployment-level secrets and runtime environment variables.
2. `app_config.json`
   Initial seed configuration used to create the first runtime settings row.
3. `app_settings` table
   The effective runtime source of truth after bootstrap.

The admin settings page can manage:

- app identity
- security settings
- auth settings
- email settings
- logging settings
- dashboard settings
- theme and visual settings
- add-on policies
- API platform settings

Runtime settings can also be exported and imported:

```bash
python cli.py settings-export ./settings_export.json
python cli.py settings-import ./settings_export.json
```

## Environment Notes

Notable variables from `.env.example`:

- `APP_ENV`
- `SECRET_KEY`
- `DATABASE_URL`
- `TEST_DATABASE_URL`
- `HOST`
- `PORT`
- `GUNICORN_WORKERS`
- `GUNICORN_THREADS`
- `GUNICORN_TIMEOUT`
- `RATELIMIT_STORAGE_URI`
- `PROXY_FIX_X_FOR`
- `PROXY_FIX_X_PROTO`
- `PROXY_FIX_X_HOST`
- `PROXY_FIX_X_PORT`

If the app runs behind a reverse proxy, configure the `PROXY_FIX_*` values correctly so request metadata is trusted.

## Host Allow-List

The application enforces host-header validation.

- In development, `DEV_ALLOW_ALL_HOSTS=true` allows flexible LAN testing.
- In production, wildcard host allowance is not accepted.
- If `SECURITY.ALLOWED_HOSTS` is not explicitly configured in runtime settings, the project derives a safe default from `SETTINGS.BASE_URL`.

Supported patterns:

- exact hostnames or IPs
- wildcard subdomains such as `*.example.com`
- CIDR ranges such as `192.168.1.0/24`

## API Surface

The FastAPI layer is intentionally separate from the Flask UI and is designed for third-party integrations and programmatic access.

Default routes include:

- `/v1/health`
- `/v1/meta`
- `/v1/meta/routes`
- `/v1/auth/me`
- `/v1/auth/token`

Runtime controls include:

- API enable/disable
- public base URL
- docs path
- OpenAPI path
- ReDoc path
- browser CORS allow-list

Production recommendation:

- disable docs and OpenAPI unless they are explicitly required

## Personal API Keys

Users can manage their own personal API keys from the account settings page.

Current behavior:

- keys are created per user
- raw tokens are revealed only once
- users can revoke their own keys
- temporary tester tokens are kept separate from long-lived personal keys

API Tester integration details:

- the add-on loads account key inventory from `GET /auth/api-keys/data`
- personal key creation inside the add-on uses `POST /auth/api-keys/create.json`
- the tester refuses bearer tokens owned by another user
- temporary `api_tester` tokens are short-lived and system-managed

## Background Jobs

The project includes a job runtime for asynchronous work such as email delivery and queued operations.

Operational support includes:

- job launch and monitoring
- queue controls
- runtime visibility
- worker health indicators
- recent job inspection from the admin interface

## Add-On System

Add-ons can extend the platform with:

- Flask pages
- admin views
- navigation items
- FastAPI mounts
- add-on-specific configuration

ZIP-installed add-ons are validated before installation and must be considered trusted code. Installing an add-on is equivalent to deploying Python code on the server.

Important operational detail:

- new Flask routes from a freshly installed add-on require restart to become active
- add-on routes should log operational failures through [app/services/app_logger.py](/home/matteo/PycharmProjects/webApp/app/services/app_logger.py) and use audit entries for user-visible lifecycle events

## Database Administration

The admin database page provides operational tooling for PostgreSQL-oriented deployments, including:

- database overview
- table diagnostics
- runtime settings metadata
- read-only SQL queries for inspection
- maintenance actions
- export helpers

## Localization

The UI supports:

- English
- Italian

Language resolution considers:

- query parameter
- session preference
- authenticated user preference
- browser language headers

Default behavior:

- English is the application fallback when no higher-priority preference is available

## Testing

Basic test run:

```bash
pytest -q
```

Coverage notes:

- [tests/test_api_tester.py](/home/matteo/PycharmProjects/webApp/tests/test_api_tester.py) covers the API Tester auth workspace routes and token ownership checks

Full-stack stress run:

```bash
python tests/test_all.py
```

The stress test expects:

- PostgreSQL to be reachable
- required Python dependencies installed
- Playwright available
- a valid bootstrap flow

## Repository Paths

- [app](/home/matteo/PycharmProjects/webApp/app)
- [addons](/home/matteo/PycharmProjects/webApp/addons)
- [seed/seed.json](/home/matteo/PycharmProjects/webApp/seed/seed.json)
- [app_config.json](/home/matteo/PycharmProjects/webApp/app_config.json)
- [ui_template](/home/matteo/PycharmProjects/webApp/ui_template)
- [tests/test_all.py](/home/matteo/PycharmProjects/webApp/tests/test_all.py)

## Documentation

- [UI](docs/UI.md)
- [Add-ons](docs/ADDONS.md)
- [Add-on Functions](docs/ADDON_FUNCTIONS.md)
- [Add-on Tutorial](docs/ADDON_TUTORIAL.md)
- [Codebase](docs/CODEBASE.md)
- [Security](docs/SECURITY.md)

## Security Notes

- PostgreSQL is required; SQLite is not part of the supported runtime model.
- Rate limiting should use a shared backend such as Redis when running with multiple workers.
- Encrypted secrets and token reveal records derive from `SECRET_KEY`.
- Changing `SECRET_KEY` invalidates previously encrypted data unless re-encrypted through a dedicated migration path.

## License

See [LICENSE](/home/matteo/PycharmProjects/webApp/LICENSE).
