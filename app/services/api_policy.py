from __future__ import annotations

from flask import current_app, g, request
from flask_smorest import abort

from .api_access import get_endpoint_access_mode
from .api_auth import extract_api_token, validate_api_token
from .audit import audit


def enforce_api_access(endpoint_key: str, default_mode: str = "key_required") -> str:
    mode = get_endpoint_access_mode(endpoint_key, default_mode=default_mode)
    if mode == "off":
        abort(404, message="Endpoint disabilitato")
    if mode == "key_required":
        token = extract_api_token() or ""
        tok = validate_api_token(token)
        if tok is None:
            sec = dict(current_app.config.get("SECURITY", {}) or {})
            if bool(sec.get("SECURITY_AUDIT_ENABLED", True)):
                reason = "missing_token" if not token else "invalid_token"
                audit(
                    "security.api_auth_failed",
                    "API token validation failed",
                    level="WARNING",
                    context={
                        "endpoint_key": endpoint_key,
                        "reason": reason,
                        "path": request.path,
                        "method": request.method,
                        "token_prefix": token[:16] if token else None,
                    },
                )
            abort(401, message="Token API mancante o non valido")
        g.api_token_id = tok.id
        g.api_user = tok.user
    return mode
