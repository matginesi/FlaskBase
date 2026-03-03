from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import current_app

from ..extensions import db
from ..models import AddonRegistry, ApiToken, BroadcastMessage, JobQueue, User, now_utc
from .addon_data_service import ensure_addon_registry, grant_addon_capability, record_addon_install_event, set_addon_config
from .app_settings_service import ensure_app_settings_row


def _seed_path() -> Path:
    configured = str(current_app.config.get("SEED_PATH", "seed/seed.json")).strip() or "seed/seed.json"
    path = Path(configured)
    if not path.is_absolute():
        path = Path(current_app.root_path).parent / path
    return path


def load_seed_data() -> dict[str, Any]:
    path = _seed_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def seed_runtime_settings() -> None:
    ensure_app_settings_row()
    seed = load_seed_data()
    seed_addon_registry(seed)
    seed_job_queues(seed)
    seed_broadcasts(seed)


def seed_users() -> list[dict[str, Any]]:
    seed = load_seed_data()
    users = seed.get("users", [])
    created: list[dict[str, Any]] = []
    for row in users if isinstance(users, list) else []:
        email = str(row.get("email", "")).strip().lower()
        if not email:
            continue
        password = str(row.get("password", "") or "changeme")
        user = User.query.filter_by(email=email).first()
        is_new = user is None
        if user is None:
            user = User(
                email=email,
                name=str(row.get("name", email.split("@", 1)[0])).strip() or email.split("@", 1)[0],
                role=str(row.get("role", "user")).strip() or "user",
                is_active=True,
                email_verified=True,
                account_status="active",
                signup_source="seed",
            )
            db.session.add(user)

        user.name = str(row.get("name", user.name)).strip() or user.name
        user.role = str(row.get("role", user.role)).strip() or user.role
        user.is_active = bool(row.get("is_active", True))
        user.email_verified = bool(row.get("email_verified", True))
        user.account_status = str(row.get("account_status", "active")).strip() or "active"
        user.username = str(row.get("username", "")).strip()[:80] or user.username
        user.company = str(row.get("company", "")).strip()[:120] or user.company
        user.department = str(row.get("department", "")).strip()[:120] or user.department
        user.job_title = str(row.get("job_title", "")).strip()[:120] or user.job_title
        user.locale = str(row.get("locale", "en")).strip()[:16] or "en"
        user.timezone = str(row.get("timezone", "Europe/Rome")).strip()[:64] or "Europe/Rome"
        user.failed_login_count = 0
        user.locked_until = None
        user.set_password(password)

        db.session.flush()

        token_name = str(row.get("api_token_name", f"seed-{user.role}")).strip() or f"seed-{user.role}"
        token = ApiToken.query.filter_by(user_id=user.id, name=token_name, revoked_at=None).first()
        raw_token = None
        if token is None:
            token, raw_token = ApiToken.create(
                user_id=user.id,
                name=token_name,
                addon_key=str(row.get("api_token_addon", "")).strip() or None,
                scopes=list(row.get("api_scopes", []) or []),
            )
            db.session.add(token)

        created.append(
            {
                "email": email,
                "role": user.role,
                "api_token": raw_token,
                "created": is_new,
            }
        )

    db.session.commit()
    return created


def seed_addon_registry(seed: dict[str, Any] | None = None) -> list[str]:
    payload = seed or load_seed_data()
    rows = payload.get("addons", [])
    seeded: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        addon_key = str(row.get("addon_key", "") or row.get("id", "")).strip().lower()
        if not addon_key:
            continue
        registry = ensure_addon_registry(
            addon_key,
            title=str(row.get("title", addon_key)).strip() or addon_key,
            version=str(row.get("version", "1.0.0")).strip() or "1.0.0",
            description=str(row.get("description", "")).strip(),
            source_type=str(row.get("source_type", "seed")).strip() or "seed",
            source_path=str(row.get("source_path", "")).strip() or None,
            is_builtin=bool(row.get("is_builtin", False)),
            status=str(row.get("status", "seeded")).strip() or "seeded",
            config_json=dict(row.get("config_defaults", {}) or {}),
            visual_json=dict(row.get("visual_defaults", {}) or {}),
            manifest_json=dict(row.get("manifest", {}) or {}),
        )
        registry.installed_at = registry.installed_at or now_utc()
        registry.is_enabled = bool(row.get("enabled", True))
        set_addon_config(addon_key, "default", dict(row.get("config_defaults", {}) or {}), scope="global")
        for grant in row.get("grants", []) if isinstance(row.get("grants"), list) else []:
            grant_addon_capability(
                addon_key,
                principal_type=str(grant.get("principal_type", "role")).strip() or "role",
                principal_value=str(grant.get("principal_value", "")).strip(),
                capability=str(grant.get("capability", "")).strip(),
                is_allowed=bool(grant.get("is_allowed", True)),
                notes=str(grant.get("notes", "")).strip() or None,
            )
        record_addon_install_event(
            addon_key,
            action="seed",
            status="ok",
            source="seed.json",
            message="Addon registry seeded",
            payload_json={"enabled": registry.is_enabled},
        )
        seeded.append(addon_key)
    db.session.commit()
    return seeded


def seed_job_queues(seed: dict[str, Any] | None = None) -> list[str]:
    payload = seed or load_seed_data()
    queues = payload.get("job_queues", [])
    seeded: list[str] = []
    for row in queues if isinstance(queues, list) else []:
        queue_key = str(row.get("queue_key", "")).strip().lower()
        if not queue_key:
            continue
        existing = JobQueue.query.filter_by(queue_key=queue_key).first()
        addon_id = None
        addon_key = str(row.get("addon_key", "")).strip().lower()
        if addon_key:
            addon = AddonRegistry.query.filter_by(addon_key=addon_key).first()
            if addon:
                addon_id = addon.id
        if existing is None:
            existing = JobQueue(queue_key=queue_key, name=str(row.get("name", queue_key)).strip() or queue_key)
            db.session.add(existing)
        existing.addon_id = addon_id
        existing.name = str(row.get("name", existing.name)).strip() or existing.name
        existing.enabled = bool(row.get("enabled", True))
        existing.paused = bool(row.get("paused", False))
        existing.concurrency = max(1, min(int(row.get("concurrency", 1) or 1), 32))
        existing.settings_json = dict(row.get("settings", {}) or {})
        seeded.append(queue_key)
    db.session.commit()
    return seeded


def seed_broadcasts(seed: dict[str, Any] | None = None) -> list[str]:
    payload = seed or load_seed_data()
    broadcasts = payload.get("broadcasts", [])
    seeded: list[str] = []
    for row in broadcasts if isinstance(broadcasts, list) else []:
        title = str(row.get("title", "")).strip()
        body = str(row.get("body", "")).strip()
        if not title or not body:
            continue
        existing = BroadcastMessage.query.filter_by(title=title, body=body).first()
        if existing is None:
            existing = BroadcastMessage(title=title, body=body)
            db.session.add(existing)
        existing.level = str(row.get("level", "info")).strip() or "info"
        existing.is_active = bool(row.get("is_active", True))
        expires_days = int(row.get("expires_in_days", 0) or 0)
        existing.expires_at = now_utc() + timedelta(days=expires_days) if expires_days > 0 else None
        seeded.append(title)
    db.session.commit()
    return seeded
