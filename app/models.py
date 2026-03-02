from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, has_app_context
from flask_login import UserMixin
from sqlalchemy import JSON, ForeignKey, LargeBinary, UniqueConstraint, func
from werkzeug.security import check_password_hash

from .extensions import db, login_manager


ADDON_KEY_RE = re.compile(r"^[a-z0-9_][a-z0-9_\-]{1,63}$")
_PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _derive_fernet_key(secret_key: str, domain: str) -> Fernet:
    """Derive a domain-separated Fernet key from SECRET_KEY.

    Existing ciphertext generated before this HKDF migration must be re-encrypted
    by a dedicated migration utility. Rotating SECRET_KEY invalidates encrypted
    MFA secrets, add-on secrets, and API token reveal records.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=str(domain).encode("utf-8"),
    )
    key = hkdf.derive(str(secret_key or "").encode("utf-8"))
    return Fernet(urlsafe_b64encode(key))


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=now_utc, onupdate=now_utc)


class User(db.Model, UserMixin, TimestampMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user", index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    account_status = db.Column(db.String(24), nullable=False, default="active", index=True)
    email_verified = db.Column(db.Boolean, nullable=False, default=False, index=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    pending_email = db.Column(db.String(255), nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=True, index=True)
    phone = db.Column(db.String(32), nullable=True)
    company = db.Column(db.String(120), nullable=True)
    department = db.Column(db.String(120), nullable=True)
    job_title = db.Column(db.String(120), nullable=True)
    locale = db.Column(db.String(16), nullable=True, default="it-IT")
    timezone = db.Column(db.String(64), nullable=True, default="Europe/Rome")
    country = db.Column(db.String(80), nullable=True)
    city = db.Column(db.String(120), nullable=True)
    postal_code = db.Column(db.String(20), nullable=True)
    address_line1 = db.Column(db.String(180), nullable=True)
    address_line2 = db.Column(db.String(180), nullable=True)
    birth_date = db.Column(db.Date, nullable=True)
    avatar_url = db.Column(db.String(500), nullable=True)
    emergency_contact_name = db.Column(db.String(120), nullable=True)
    emergency_contact_phone = db.Column(db.String(32), nullable=True)
    notification_email_enabled = db.Column(db.Boolean, nullable=False, default=True)
    notification_security_enabled = db.Column(db.Boolean, nullable=False, default=True)
    marketing_consent = db.Column(db.Boolean, nullable=False, default=False)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    privacy_accepted_at = db.Column(db.DateTime, nullable=True)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secret_enc = db.Column(db.String(600), nullable=True)
    mfa_recovery_hashes = db.Column(JSON, nullable=True)
    mfa_enrolled_at = db.Column(db.DateTime, nullable=True)
    mfa_last_used_at = db.Column(db.DateTime, nullable=True)
    mfa_recovery_used_count = db.Column(db.Integer, nullable=False, default=0)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True, index=True)
    signup_source = db.Column(db.String(40), nullable=True, default="admin")
    last_password_change_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True, index=True)
    last_ip = db.Column(db.String(64), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    deactivated_at = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, password: str) -> None:
        self.password_hash = _PASSWORD_HASHER.hash(str(password))

    def check_password(self, password: str) -> bool:
        raw_hash = str(self.password_hash or "")
        if raw_hash.startswith("$argon2"):
            try:
                ok = _PASSWORD_HASHER.verify(raw_hash, str(password))
                if ok and _PASSWORD_HASHER.check_needs_rehash(raw_hash):
                    self.set_password(password)
                return bool(ok)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False
        ok = check_password_hash(raw_hash, str(password))
        if ok:
            self.set_password(password)
        return ok

    def get_id(self) -> str:
        return str(self.id)

    def is_admin(self) -> bool:
        return self.role == "admin"


class UserSession(db.Model, TimestampMixin):
    __tablename__ = "user_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    refresh_token_hash = db.Column(db.String(64), nullable=True, unique=True, index=True)
    csrf_salt = db.Column(db.String(64), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    auth_method = db.Column(db.String(24), nullable=False, default="password", index=True)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    last_seen_at = db.Column(db.DateTime, nullable=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True, index=True)
    metadata_json = db.Column(JSON, nullable=True)

    user = db.relationship("User", backref="sessions")

    @staticmethod
    def hash_token(raw_token: str) -> str:
        digest = hashlib.sha256(str(raw_token or "").encode("utf-8"))
        return digest.hexdigest()

    @classmethod
    def create_session(
        cls,
        *,
        user_id: int,
        ip: str | None = None,
        user_agent: str | None = None,
        auth_method: str = "password",
        ttl_minutes: int = 120,
    ) -> tuple["UserSession", str]:
        raw = "sess_" + secrets.token_urlsafe(36)
        row = cls(
            user_id=int(user_id),
            session_token_hash=cls.hash_token(raw),
            csrf_salt=secrets.token_urlsafe(16),
            ip=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:255] or None,
            auth_method=(auth_method or "password")[:24],
            last_seen_at=now_utc(),
            expires_at=now_utc() + timedelta(minutes=max(5, min(int(ttl_minutes or 120), 43200))),
        )
        return row, raw

    def is_valid(self) -> bool:
        now = now_utc()
        if self.revoked_at is not None or self.status != "active":
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True


class LogEvent(db.Model):
    __tablename__ = "log_events"

    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, nullable=False, server_default=func.now(), index=True)
    level = db.Column(db.String(16), nullable=False, default="INFO", index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    message = db.Column(db.String(500), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    ip = db.Column(db.String(64), nullable=True)
    path = db.Column(db.String(200), nullable=True)
    method = db.Column(db.String(12), nullable=True)
    addon_key = db.Column(db.String(64), nullable=True, index=True)
    request_id = db.Column(db.String(64), nullable=True, index=True)
    context = db.Column(JSON, nullable=True)

    user = db.relationship("User", backref="log_events")


class ApiToken(db.Model, TimestampMixin):
    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    addon_key = db.Column(db.String(64), nullable=True, index=True)
    name = db.Column(db.String(80), nullable=False, default="default")
    token_prefix = db.Column(db.String(12), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    scopes_json = db.Column(JSON, nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    user = db.relationship("User", foreign_keys=[user_id], backref="api_tokens")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], backref="issued_api_tokens")

    @staticmethod
    def generate_raw_token() -> str:
        return "sfk_" + secrets.token_urlsafe(36)

    @staticmethod
    def hash_token(raw_token: str) -> str:
        digest = hashlib.sha256(str(raw_token or "").encode("utf-8"))
        return digest.hexdigest()

    @classmethod
    def create(
        cls,
        user_id: int,
        name: str = "default",
        expires_at: Optional[datetime] = None,
        *,
        addon_key: str | None = None,
        created_by_user_id: int | None = None,
        scopes: list[str] | None = None,
    ) -> tuple["ApiToken", str]:
        raw = cls.generate_raw_token()
        token = cls(
            user_id=int(user_id),
            addon_key=(addon_key or "")[:64] or None,
            name=(name or "default")[:80],
            token_prefix=raw[:12],
            token_hash=cls.hash_token(raw),
            expires_at=expires_at,
            created_by_user_id=created_by_user_id,
            scopes_json=list(scopes or []),
        )
        return token, raw

    def verify(self, raw_token: str) -> bool:
        return hmac.compare_digest(self.token_hash, self.hash_token(raw_token))

    def is_valid(self) -> bool:
        now = now_utc()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True


class AppSettings(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True, default=1)
    app_name = db.Column(db.String(80), nullable=False, default="WebApp")
    app_version = db.Column(db.String(32), nullable=False, default="1.0.0")
    base_url = db.Column(db.String(220), nullable=False, default="http://127.0.0.1:5000")
    settings_json = db.Column(db.JSON, nullable=False, default=dict)
    addons_json = db.Column(db.JSON, nullable=False, default=dict)
    pages_json = db.Column(db.JSON, nullable=True)
    theme_json = db.Column(db.JSON, nullable=False, default=dict)
    visual_json = db.Column(db.JSON, nullable=False, default=dict)
    revision = db.Column(db.Integer, nullable=False, default=1)
    seed_source = db.Column(db.String(255), nullable=True)
    seed_checksum = db.Column(db.String(64), nullable=True)
    last_imported_at = db.Column(db.DateTime, nullable=True)
    last_exported_at = db.Column(db.DateTime, nullable=True)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    updated_by_user = db.relationship("User", foreign_keys=[updated_by_user_id], backref="settings_updates")

    @property
    def config_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "SETTINGS": {
                "APP_NAME": self.app_name,
                "APP_VERSION": self.app_version,
                "BASE_URL": self.base_url,
            },
            "ADDONS": dict(self.addons_json or {}),
        }
        payload["SETTINGS"].update(dict(self.settings_json or {}))
        if isinstance(self.pages_json, dict):
            payload["PAGES"] = dict(self.pages_json or {})
        return payload

    @config_json.setter
    def config_json(self, value: dict[str, Any] | None) -> None:
        data = dict(value or {})
        settings = dict(data.get("SETTINGS") or {})
        self.app_name = str(settings.get("APP_NAME", self.app_name or "WebApp")).strip() or "WebApp"
        self.app_version = str(settings.get("APP_VERSION", self.app_version or "1.0.0")).strip() or "1.0.0"
        self.base_url = str(settings.get("BASE_URL", self.base_url or "http://127.0.0.1:5000")).strip() or "http://127.0.0.1:5000"
        self.settings_json = {
            key: val
            for key, val in settings.items()
            if key not in {"APP_NAME", "APP_VERSION", "BASE_URL"}
        }
        self.addons_json = dict(data.get("ADDONS") or {})
        self.pages_json = dict(data.get("PAGES") or {}) if isinstance(data.get("PAGES"), dict) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "app_name": self.app_name,
            "app_version": self.app_version,
            "base_url": self.base_url,
            "settings_json": dict(self.settings_json or {}),
            "addons_json": dict(self.addons_json or {}),
            "pages_json": dict(self.pages_json or {}) if isinstance(self.pages_json, dict) else None,
            "config_json": dict(self.config_json or {}),
            "theme_json": dict(self.theme_json or {}),
            "visual_json": dict(self.visual_json or {}),
            "revision": int(self.revision or 1),
            "seed_source": self.seed_source,
            "seed_checksum": self.seed_checksum,
            "last_imported_at": self.last_imported_at.isoformat() if self.last_imported_at else None,
            "last_exported_at": self.last_exported_at.isoformat() if self.last_exported_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AddonRegistry(db.Model, TimestampMixin):
    __tablename__ = "addon_registry"

    id = db.Column(db.Integer, primary_key=True)
    addon_key = db.Column(db.String(64), nullable=False, unique=True, index=True)
    title = db.Column(db.String(120), nullable=False)
    version = db.Column(db.String(40), nullable=False, default="1.0.0")
    description = db.Column(db.Text, nullable=True)
    source_type = db.Column(db.String(24), nullable=False, default="builtin", index=True)
    source_path = db.Column(db.String(255), nullable=True)
    checksum_sha256 = db.Column(db.String(64), nullable=True)
    min_app_version = db.Column(db.String(40), nullable=True)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    is_builtin = db.Column(db.Boolean, nullable=False, default=False, index=True)
    status = db.Column(db.String(24), nullable=False, default="available", index=True)
    last_loaded_at = db.Column(db.DateTime, nullable=True)
    installed_at = db.Column(db.DateTime, nullable=True)
    installed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    manifest_json = db.Column(JSON, nullable=True)
    config_json = db.Column(JSON, nullable=True)
    visual_json = db.Column(JSON, nullable=True)

    installed_by_user = db.relationship("User", backref="installed_addons")

    @staticmethod
    def normalize_key(raw: str) -> str:
        clean = str(raw or "").strip().lower()
        if not ADDON_KEY_RE.match(clean):
            raise ValueError("addon_key non valido")
        return clean


class AddonInstallEvent(db.Model, TimestampMixin):
    __tablename__ = "addon_install_events"

    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = db.Column(db.String(24), nullable=False, index=True)  # install|update|enable|disable|uninstall|seed
    status = db.Column(db.String(20), nullable=False, default="ok", index=True)
    source = db.Column(db.String(40), nullable=True)
    message = db.Column(db.String(255), nullable=True)
    payload_json = db.Column(JSON, nullable=True)

    addon = db.relationship("AddonRegistry", backref="install_events")
    actor_user = db.relationship("User", backref="addon_install_events")


class AddonConfig(db.Model, TimestampMixin):
    __tablename__ = "addon_configs"

    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="CASCADE"), nullable=False, index=True)
    scope = db.Column(db.String(16), nullable=False, default="global", index=True)  # global|user
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    config_key = db.Column(db.String(120), nullable=False)
    config_json = db.Column(JSON, nullable=False, default=dict)
    revision = db.Column(db.Integer, nullable=False, default=1)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("addon_id", "scope", "user_id", "config_key", name="uq_addon_config_scope_key"),
    )

    addon = db.relationship("AddonRegistry", backref="config_entries")
    user = db.relationship("User", foreign_keys=[user_id], backref="addon_configs")
    updated_by_user = db.relationship("User", foreign_keys=[updated_by_user_id], backref="addon_configs_updated")


class AddonSecret(db.Model, TimestampMixin):
    __tablename__ = "addon_secrets"

    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="CASCADE"), nullable=False, index=True)
    scope = db.Column(db.String(16), nullable=False, default="global", index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    secret_key = db.Column(db.String(120), nullable=False)
    secret_ciphertext = db.Column(db.Text, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    last_rotated_at = db.Column(db.DateTime, nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("addon_id", "scope", "user_id", "secret_key", name="uq_addon_secret_scope_key"),
    )

    addon = db.relationship("AddonRegistry", backref="secret_entries")
    user = db.relationship("User", foreign_keys=[user_id], backref="addon_secrets")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], backref="addon_secrets_created")
    updated_by_user = db.relationship("User", foreign_keys=[updated_by_user_id], backref="addon_secrets_updated")

    @staticmethod
    def _fernet(secret_key: str) -> Fernet:
        return _derive_fernet_key(secret_key, "addon-secret-v1")

    def set_secret(self, raw_value: str, secret_key: str) -> None:
        self.secret_ciphertext = self._fernet(secret_key).encrypt(str(raw_value or "").encode("utf-8")).decode("utf-8")
        self.last_rotated_at = now_utc()

    def get_secret(self, secret_key: str) -> Optional[str]:
        try:
            data = self._fernet(secret_key).decrypt(self.secret_ciphertext.encode("utf-8"))
            return data.decode("utf-8")
        except (InvalidToken, ValueError):
            return None


class AddonGrant(db.Model, TimestampMixin):
    __tablename__ = "addon_grants"

    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="CASCADE"), nullable=False, index=True)
    principal_type = db.Column(db.String(16), nullable=False, default="role", index=True)  # role|user
    principal_value = db.Column(db.String(120), nullable=False, index=True)
    capability = db.Column(db.String(120), nullable=False, index=True)
    is_allowed = db.Column(db.Boolean, nullable=False, default=True, index=True)
    notes = db.Column(db.String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("addon_id", "principal_type", "principal_value", "capability", name="uq_addon_grant_cap"),
    )

    addon = db.relationship("AddonRegistry", backref="grants")


class AddonDataObject(db.Model, TimestampMixin):
    __tablename__ = "addon_data_objects"

    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    scope = db.Column(db.String(16), nullable=False, default="global", index=True)
    bucket = db.Column(db.String(64), nullable=False, index=True)
    object_key = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120), nullable=True)
    encoding = db.Column(db.String(32), nullable=True)
    is_encrypted = db.Column(db.Boolean, nullable=False, default=False)
    checksum_sha256 = db.Column(db.String(64), nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    text_value = db.Column(db.Text, nullable=True)
    bytes_value = db.Column(LargeBinary, nullable=True)
    json_value = db.Column(JSON, nullable=True)
    metadata_json = db.Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("addon_id", "scope", "owner_user_id", "bucket", "object_key", name="uq_addon_data_object"),
    )

    addon = db.relationship("AddonRegistry", backref="data_objects")
    owner_user = db.relationship("User", backref="addon_data_objects")


class ApiTokenReveal(db.Model, TimestampMixin):
    __tablename__ = "api_token_reveals"

    id = db.Column(db.Integer, primary_key=True)
    token_id = db.Column(db.Integer, db.ForeignKey("api_tokens.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    token_prefix = db.Column(db.String(12), nullable=False, index=True)
    token_ciphertext = db.Column(db.Text, nullable=False)
    name = db.Column(db.String(80), nullable=False, default="default")
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    revealed_at = db.Column(db.DateTime, nullable=True, index=True)

    token = db.relationship("ApiToken", backref="reveals")
    user = db.relationship("User", foreign_keys=[user_id], backref="api_token_reveals")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])

    @staticmethod
    def _fernet(secret_key: str) -> Fernet:
        return _derive_fernet_key(secret_key, "api-token-reveal-v1")

    @classmethod
    def create_encrypted(
        cls,
        *,
        user_id: int,
        raw_token: str,
        token_prefix: str,
        secret_key: str,
        name: str = "default",
        expires_at: Optional[datetime] = None,
        created_by_user_id: Optional[int] = None,
        token_id: Optional[int] = None,
    ) -> "ApiTokenReveal":
        cipher = cls._fernet(secret_key).encrypt(str(raw_token or "").encode("utf-8")).decode("utf-8")
        return cls(
            token_id=token_id,
            user_id=user_id,
            created_by_user_id=created_by_user_id,
            token_prefix=token_prefix[:12],
            token_ciphertext=cipher,
            name=(name or "default")[:80],
            expires_at=expires_at,
        )

    def decrypt_raw(self, secret_key: str) -> Optional[str]:
        try:
            data = self._fernet(secret_key).decrypt(self.token_ciphertext.encode("utf-8"))
            return data.decode("utf-8")
        except (InvalidToken, ValueError):
            return None


class EmailVerificationToken(db.Model):
    __tablename__ = "email_verification_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    sent_to_email = db.Column(db.String(255), nullable=False)
    purpose = db.Column(db.String(40), nullable=False, default="signup_confirm")
    token_prefix = db.Column(db.String(16), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True, index=True)

    user = db.relationship("User", backref="email_verification_tokens")

    @staticmethod
    def hash_token(raw_token: str) -> str:
        digest = hashlib.sha256(str(raw_token or "").encode("utf-8"))
        return digest.hexdigest()

    @classmethod
    def create_token(
        cls,
        *,
        user_id: int,
        sent_to_email: str,
        purpose: str = "signup_confirm",
        ttl_minutes: int = 60,
    ) -> tuple["EmailVerificationToken", str]:
        raw = "emc_" + secrets.token_urlsafe(36)
        row = cls(
            user_id=user_id,
            sent_to_email=(sent_to_email or "").strip().lower()[:255],
            purpose=(purpose or "signup_confirm")[:40],
            token_prefix=raw[:16],
            token_hash=cls.hash_token(raw),
            expires_at=now_utc() + timedelta(minutes=max(5, min(int(ttl_minutes or 60), 1440))),
        )
        return row, raw

    def is_valid(self) -> bool:
        return self.consumed_at is None and self.expires_at > now_utc()


class JobQueue(db.Model, TimestampMixin):
    __tablename__ = "job_queues"

    id = db.Column(db.Integer, primary_key=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="SET NULL"), nullable=True, index=True)
    queue_key = db.Column(db.String(40), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    paused = db.Column(db.Boolean, nullable=False, default=False, index=True)
    concurrency = db.Column(db.Integer, nullable=False, default=1)
    settings_json = db.Column(JSON, nullable=True)

    addon = db.relationship("AddonRegistry", backref="job_queues")


class JobRun(db.Model):
    __tablename__ = "job_runs"

    id = db.Column(db.Integer, primary_key=True)
    queue_id = db.Column(db.Integer, db.ForeignKey("job_queues.id", ondelete="CASCADE"), nullable=False, index=True)
    addon_id = db.Column(db.Integer, db.ForeignKey("addon_registry.id", ondelete="SET NULL"), nullable=True, index=True)
    requested_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    job_type = db.Column(db.String(80), nullable=False, index=True)
    addon_job_key = db.Column(db.String(120), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="queued", index=True)
    progress = db.Column(db.Integer, nullable=False, default=0)
    stop_requested = db.Column(db.Boolean, nullable=False, default=False, index=True)
    message = db.Column(db.String(250), nullable=True)
    payload = db.Column(JSON, nullable=True)
    result = db.Column(JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    started_at = db.Column(db.DateTime, nullable=True, index=True)
    finished_at = db.Column(db.DateTime, nullable=True, index=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True, index=True)

    queue = db.relationship("JobQueue", backref="jobs")
    addon = db.relationship("AddonRegistry", backref="job_runs")
    requested_by_user = db.relationship("User", foreign_keys=[requested_by_user_id], backref="requested_jobs")


class BroadcastMessage(db.Model):
    __tablename__ = "broadcast_messages"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    body_format = db.Column(db.String(16), nullable=False, default="text")
    level = db.Column(db.String(16), nullable=False, default="info")
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    email_requested = db.Column(db.Boolean, nullable=False, default=False)
    email_template_key = db.Column(db.String(64), nullable=True)
    email_subject = db.Column(db.String(180), nullable=True)
    email_preheader = db.Column(db.String(180), nullable=True)
    action_label = db.Column(db.String(80), nullable=True)
    action_url = db.Column(db.String(500), nullable=True)
    email_sent_at = db.Column(db.DateTime, nullable=True)
    email_error = db.Column(db.String(255), nullable=True)

    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], backref="broadcasts_created")

    def is_visible(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_at is None:
            return True
        try:
            return now_utc() <= self.expires_at
        except Exception:
            return True


class BroadcastMessageRead(db.Model):
    __tablename__ = "broadcast_message_reads"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("broadcast_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    read_at = db.Column(db.DateTime, nullable=False, default=now_utc)

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_broadcast_read_user_message"),
    )

    message = db.relationship("BroadcastMessage", backref=db.backref("reads", lazy="dynamic"))
    user = db.relationship("User", backref=db.backref("broadcast_reads", lazy="dynamic"))


class UserMessage(db.Model):
    __tablename__ = "user_messages"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=now_utc, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    body_format = db.Column(db.String(16), nullable=False, default="text")
    level = db.Column(db.String(16), nullable=False, default="info")
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    read_at = db.Column(db.DateTime, nullable=True)
    email_requested = db.Column(db.Boolean, nullable=False, default=False)
    email_template_key = db.Column(db.String(64), nullable=True)
    email_subject = db.Column(db.String(180), nullable=True)
    email_preheader = db.Column(db.String(180), nullable=True)
    action_label = db.Column(db.String(80), nullable=True)
    action_url = db.Column(db.String(500), nullable=True)
    email_sent_at = db.Column(db.DateTime, nullable=True)
    email_error = db.Column(db.String(255), nullable=True)

    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], backref="user_messages_created")
    user = db.relationship("User", foreign_keys=[user_id], backref="messages")

    def is_visible(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_at is None:
            return True
        try:
            return now_utc() <= self.expires_at
        except Exception:
            return True


@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


def ensure_runtime_schema_updates() -> None:
    return None


def app_secret_key() -> str:
    if has_app_context():
        return str(current_app.config.get("SECRET_KEY", "") or "")
    return ""
