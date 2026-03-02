from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar, cast

from flask import abort
from flask_login import current_user, login_required

F = TypeVar("F", bound=Callable[..., object])


def _user_is_fully_active(user) -> bool:
    """Return True only if the user account is fully active and not locked."""
    if not getattr(user, "is_active", False):
        return False
    status = str(getattr(user, "account_status", "active") or "active").lower()
    if status not in ("active",):
        return False
    return True


def roles_required(*roles: str) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if not _user_is_fully_active(current_user):
                abort(403)
            if getattr(current_user, "role", None) not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return cast(F, wrapper)
    return decorator
