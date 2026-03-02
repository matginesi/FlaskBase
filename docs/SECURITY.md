# Security

## Principles

- PostgreSQL only
- secrets stored in `.env`
- non-sensitive runtime settings stored in the database
- structured logging with redaction for sensitive values
- FastAPI served separately from the Flask UI for third-party integrations

## Essential Variables

- `SECRET_KEY`
- `DATABASE_URL`
- `TEST_DATABASE_URL`
- `SESSION_COOKIE_SECURE`
- `REMEMBER_COOKIE_SECURE`

## Enabled Protections

- CSRF on Flask forms
- `HttpOnly` cookies
- CSP and security headers
- login rate limiting
- temporary lock on repeated failed logins
- audit trail for auth and admin events
- bearer-token validation against `api_tokens`
- FastAPI host-header validation against the runtime allow-list
- FastAPI request-size enforcement aligned with runtime security limits
- add-on API mounts gated by token auth, roles, and optional scopes

## Operational Notes

- do not use demo credentials in real environments
- do not keep `memory://` as the rate limit backend in distributed production
- use HTTPS in front of Gunicorn / reverse proxy
