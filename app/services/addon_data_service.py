from __future__ import annotations

import hashlib
import json
from typing import Any

from flask import current_app

from ..extensions import db
from ..models import AddonConfig, AddonDataObject, AddonGrant, AddonInstallEvent, AddonRegistry, AddonSecret, now_utc


def _addon_key(raw: str) -> str:
    return AddonRegistry.normalize_key(raw)


def ensure_addon_registry(
    addon_key: str,
    *,
    title: str,
    version: str = "1.0.0",
    description: str = "",
    source_type: str = "builtin",
    source_path: str | None = None,
    is_builtin: bool = False,
    status: str = "available",
    manifest_json: dict[str, Any] | None = None,
    config_json: dict[str, Any] | None = None,
    visual_json: dict[str, Any] | None = None,
) -> AddonRegistry:
    clean = _addon_key(addon_key)
    row = AddonRegistry.query.filter_by(addon_key=clean).first()
    if row is None:
        row = AddonRegistry(addon_key=clean, title=title[:120] or clean)
        db.session.add(row)
    row.title = title[:120] or clean
    row.version = (version or "1.0.0")[:40]
    row.description = description or None
    row.source_type = (source_type or "builtin")[:24]
    row.source_path = (source_path or "")[:255] or None
    row.is_builtin = bool(is_builtin)
    row.status = (status or "available")[:24]
    if row.installed_at is None and row.source_type in {"builtin", "seed", "zip"}:
        row.installed_at = now_utc()
    if row.status in {"loaded", "pending_restart", "seeded"}:
        row.last_loaded_at = now_utc()
    row.manifest_json = dict(manifest_json or {})
    row.config_json = dict(config_json or {})
    row.visual_json = dict(visual_json or {})
    return row


def set_addon_config(
    addon_key: str,
    config_key: str,
    config_json: dict[str, Any],
    *,
    scope: str = "global",
    user_id: int | None = None,
    updated_by_user_id: int | None = None,
) -> AddonConfig:
    addon = ensure_addon_registry(addon_key, title=addon_key)
    row = AddonConfig.query.filter_by(
        addon_id=addon.id,
        scope=(scope or "global")[:16],
        user_id=user_id,
        config_key=(config_key or "default")[:120],
    ).first()
    if row is None:
        row = AddonConfig(
            addon_id=addon.id,
            scope=(scope or "global")[:16],
            user_id=user_id,
            config_key=(config_key or "default")[:120],
            revision=1,
        )
        db.session.add(row)
    else:
        row.revision = int(row.revision or 0) + 1
    row.config_json = dict(config_json or {})
    row.updated_by_user_id = updated_by_user_id
    return row


def get_addon_config(addon_key: str, config_key: str, *, scope: str = "global", user_id: int | None = None) -> dict[str, Any] | None:
    clean = _addon_key(addon_key)
    row = (
        AddonConfig.query.join(AddonRegistry, AddonRegistry.id == AddonConfig.addon_id)
        .filter(
            AddonRegistry.addon_key == clean,
            AddonConfig.scope == (scope or "global")[:16],
            AddonConfig.user_id == user_id,
            AddonConfig.config_key == (config_key or "default")[:120],
        )
        .first()
    )
    return dict(row.config_json or {}) if row else None


def set_addon_secret(
    addon_key: str,
    secret_key_name: str,
    secret_value: str,
    *,
    scope: str = "global",
    user_id: int | None = None,
    description: str | None = None,
    actor_user_id: int | None = None,
) -> AddonSecret:
    addon = ensure_addon_registry(addon_key, title=addon_key)
    row = AddonSecret.query.filter_by(
        addon_id=addon.id,
        scope=(scope or "global")[:16],
        user_id=user_id,
        secret_key=(secret_key_name or "default")[:120],
    ).first()
    if row is None:
        row = AddonSecret(
            addon_id=addon.id,
            scope=(scope or "global")[:16],
            user_id=user_id,
            secret_key=(secret_key_name or "default")[:120],
            created_by_user_id=actor_user_id,
        )
        db.session.add(row)
    row.description = (description or "")[:255] or None
    row.updated_by_user_id = actor_user_id
    row.set_secret(secret_value, str(current_app.config.get("SECRET_KEY", "") or ""))
    return row


def get_addon_secret(addon_key: str, secret_key_name: str, *, scope: str = "global", user_id: int | None = None) -> str | None:
    clean = _addon_key(addon_key)
    row = (
        AddonSecret.query.join(AddonRegistry, AddonRegistry.id == AddonSecret.addon_id)
        .filter(
            AddonRegistry.addon_key == clean,
            AddonSecret.scope == (scope or "global")[:16],
            AddonSecret.user_id == user_id,
            AddonSecret.secret_key == (secret_key_name or "default")[:120],
        )
        .first()
    )
    if row is None:
        return None
    return row.get_secret(str(current_app.config.get("SECRET_KEY", "") or ""))


def upsert_addon_data_object(
    addon_key: str,
    *,
    bucket: str,
    object_key: str,
    scope: str = "global",
    owner_user_id: int | None = None,
    content_type: str | None = None,
    text_value: str | None = None,
    bytes_value: bytes | None = None,
    json_value: dict[str, Any] | list[Any] | None = None,
    metadata_json: dict[str, Any] | None = None,
    is_encrypted: bool = False,
) -> AddonDataObject:
    addon = ensure_addon_registry(addon_key, title=addon_key)
    row = AddonDataObject.query.filter_by(
        addon_id=addon.id,
        scope=(scope or "global")[:16],
        owner_user_id=owner_user_id,
        bucket=(bucket or "default")[:64],
        object_key=(object_key or "object")[:255],
    ).first()
    if row is None:
        row = AddonDataObject(
            addon_id=addon.id,
            scope=(scope or "global")[:16],
            owner_user_id=owner_user_id,
            bucket=(bucket or "default")[:64],
            object_key=(object_key or "object")[:255],
        )
        db.session.add(row)
    row.content_type = (content_type or "")[:120] or None
    row.text_value = text_value
    row.bytes_value = bytes_value
    row.json_value = json_value
    row.metadata_json = dict(metadata_json or {})
    row.is_encrypted = bool(is_encrypted)
    raw_bytes = b""
    if bytes_value is not None:
        raw_bytes = bytes(bytes_value)
    elif text_value is not None:
        raw_bytes = text_value.encode("utf-8")
    elif json_value is not None:
        raw_bytes = json.dumps(json_value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    row.size_bytes = len(raw_bytes)
    row.checksum_sha256 = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None
    return row


def get_addon_data_object(addon_key: str, *, bucket: str, object_key: str, scope: str = "global", owner_user_id: int | None = None) -> AddonDataObject | None:
    clean = _addon_key(addon_key)
    return (
        AddonDataObject.query.join(AddonRegistry, AddonRegistry.id == AddonDataObject.addon_id)
        .filter(
            AddonRegistry.addon_key == clean,
            AddonDataObject.scope == (scope or "global")[:16],
            AddonDataObject.owner_user_id == owner_user_id,
            AddonDataObject.bucket == (bucket or "default")[:64],
            AddonDataObject.object_key == (object_key or "object")[:255],
        )
        .first()
    )


def grant_addon_capability(addon_key: str, *, principal_type: str, principal_value: str, capability: str, is_allowed: bool = True, notes: str | None = None) -> AddonGrant:
    addon = ensure_addon_registry(addon_key, title=addon_key)
    row = AddonGrant.query.filter_by(
        addon_id=addon.id,
        principal_type=(principal_type or "role")[:16],
        principal_value=(principal_value or "")[:120],
        capability=(capability or "")[:120],
    ).first()
    if row is None:
        row = AddonGrant(
            addon_id=addon.id,
            principal_type=(principal_type or "role")[:16],
            principal_value=(principal_value or "")[:120],
            capability=(capability or "")[:120],
        )
        db.session.add(row)
    row.is_allowed = bool(is_allowed)
    row.notes = (notes or "")[:255] or None
    return row


def record_addon_install_event(addon_key: str, *, action: str, status: str = "ok", source: str | None = None, message: str | None = None, actor_user_id: int | None = None, payload_json: dict[str, Any] | None = None) -> AddonInstallEvent:
    addon = ensure_addon_registry(addon_key, title=addon_key)
    row = AddonInstallEvent(
        addon_id=addon.id,
        actor_user_id=actor_user_id,
        action=(action or "install")[:24],
        status=(status or "ok")[:20],
        source=(source or "")[:40] or None,
        message=(message or "")[:255] or None,
        payload_json=dict(payload_json or {}),
    )
    db.session.add(row)
    return row
