from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from html import escape
from urllib.parse import unquote, urlparse

import pyotp
from cryptography.fernet import InvalidToken
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ...extensions import db, limiter
from ...models import ApiToken, ApiTokenReveal, EmailVerificationToken, User, UserSession, _derive_fernet_key, now_utc
from ...services.audit import audit
from ...services.email_service import EmailServiceError, runtime_email_settings, send_email
from ...services.i18n import normalize_language, translate
from ...services.job_service import enqueue_email_job
from ...services.pages_service import get_runtime_feature_flags
from ...utils import get_client_ip, get_runtime_config_dict, get_runtime_config_value
from .forms import LoginForm, MfaVerifyForm, RegistrationForm, UserSettingsForm

bp = Blueprint("auth", __name__, url_prefix="/auth")
log = logging.getLogger(__name__)
SYSTEM_MANAGED_API_TOKEN_NAMES = {"browser-session", "api_tester"}


def _auth_cfg() -> dict:
    return dict(current_app.config.get("AUTH", {}) or {})


def _self_registration_enabled() -> bool:
    return bool(_auth_cfg().get("SELF_REGISTRATION_ENABLED", True))


def _email_confirm_required() -> bool:
    # In tests we allow login without email verification to keep suites lightweight.
    if bool(getattr(current_app, "testing", False)):
        return False
    return bool(_auth_cfg().get("EMAIL_CONFIRM_REQUIRED", True))


def _is_pending_email_confirmation(user: User | None) -> bool:
    if not user:
        return False
    if bool(user.email_verified):
        return False
    if not _email_confirm_required():
        return False
    status = str(getattr(user, "account_status", "") or "").strip().lower()
    signup_source = str(getattr(user, "signup_source", "") or "").strip().lower()
    return status == "pending_verification" or signup_source == "self_signup"


def _send_api_key_notice_on_signin_enabled() -> bool:
    """Whether to send the "API key ready" notice right after a successful sign-in.

    A previous refactor accidentally removed this helper, causing a NameError
    (and therefore sporadic 500/503 during login in some deployments).

    Priority:
    1) DB-backed runtime page/feature flags
    2) legacy FEATURES runtime fallback
    """
    try:
        flags = get_runtime_feature_flags() or {}
        if "api_key_notice_on_signin" in flags:
            return bool(flags.get("api_key_notice_on_signin"))
    except Exception:
        # Never break login because a feature flag store is unavailable.
        pass
    feats = dict(current_app.config.get("FEATURES", {}) or {})
    return bool(feats.get("API_KEY_NOTICE_ON_SIGNIN", False))




def _failed_login_limits() -> tuple[int, int]:
    cfg = _auth_cfg()
    max_attempts = max(3, min(int(cfg.get("MAX_FAILED_LOGIN_ATTEMPTS", 8)), 25))
    lock_minutes = max(1, min(int(cfg.get("LOCKOUT_MINUTES", 15)), 180))
    return max_attempts, lock_minutes


def _session_timeout_minutes() -> int:
    sec_cfg = get_runtime_config_dict("SECURITY")
    try:
        return max(5, min(int(sec_cfg.get("SESSION_TIMEOUT_MIN", 120)), 43200))
    except Exception:
        return 120


def _client_ip() -> str | None:
    return get_client_ip()


def _client_user_agent() -> str | None:
    return (request.headers.get("User-Agent") or "").strip()[:255] or None


def _browser_session_token_scopes() -> list[str]:
    return ["session:browser"]


def _issue_browser_session(user: User) -> None:
    expires_at = now_utc() + timedelta(minutes=_session_timeout_minutes())
    session_row, _raw_session_token = UserSession.create_session(
        user_id=int(user.id),
        ip=_client_ip(),
        user_agent=_client_user_agent(),
        auth_method="password+mfa" if bool(user.mfa_enabled) else "password",
        ttl_minutes=_session_timeout_minutes(),
    )
    browser_token, raw_browser_token = ApiToken.create(
        user_id=int(user.id),
        name="browser-session",
        expires_at=expires_at,
        addon_key=None,
        created_by_user_id=int(user.id),
        scopes=_browser_session_token_scopes(),
    )
    db.session.add(session_row)
    db.session.add(browser_token)
    db.session.flush()
    session["browser_session_id"] = int(session_row.id)
    session["browser_token_id"] = int(browser_token.id)
    session["browser_token_prefix"] = browser_token.token_prefix
    reveal = ApiTokenReveal.create_encrypted(
        user_id=int(user.id),
        raw_token=raw_browser_token,
        token_prefix=browser_token.token_prefix,
        secret_key=str(current_app.config.get("SECRET_KEY", "")),
        name="browser-session",
        expires_at=expires_at,
        created_by_user_id=int(user.id),
        token_id=int(browser_token.id),
    )
    reveal.revealed_at = now_utc()
    db.session.add(reveal)


def _revoke_browser_session_row(uid: int | None) -> None:
    if not uid:
        return
    browser_session_id = session.get("browser_session_id")
    if not browser_session_id:
        return
    row = db.session.get(UserSession, int(browser_session_id))
    if row and int(row.user_id) == int(uid) and row.revoked_at is None:
        row.revoked_at = now_utc()
        row.status = "revoked"


def _rehydrate_pending_token_reveals(user_id: int) -> list[dict[str, object]]:
    secret_key = str(current_app.config.get("SECRET_KEY", ""))
    rows = (
        ApiTokenReveal.query.filter_by(user_id=int(user_id), revealed_at=None)
        .order_by(ApiTokenReveal.created_at.desc())
        .all()
    )
    out: list[dict[str, object]] = []
    changed = False
    now = now_utc()
    for row in rows:
        raw = row.decrypt_raw(secret_key)
        if not raw:
            row.revealed_at = now
            changed = True
            continue
        out.append(
            {
                "id": int(row.id),
                "name": str(row.name or "default"),
                "prefix": str(row.token_prefix or ""),
                "token": raw,
                "expires_at": row.expires_at,
                "created_at": row.created_at,
            }
        )
        row.revealed_at = now
        changed = True
    if changed:
        db.session.commit()
    return out


def _non_browser_tokens_for_user(user_id: int) -> list[ApiToken]:
    return (
        ApiToken.query.filter(ApiToken.user_id == int(user_id), ApiToken.name != "browser-session")
        .order_by(ApiToken.revoked_at.isnot(None).asc(), ApiToken.created_at.desc())
        .all()
    )


def _all_user_managed_tokens_for_user(user_id: int) -> list[ApiToken]:
    rows = _non_browser_tokens_for_user(user_id)
    out: list[ApiToken] = []
    for token in rows:
        name = str(token.name or "").strip().lower()
        if name in SYSTEM_MANAGED_API_TOKEN_NAMES:
            continue
        out.append(token)
    return out


def _user_managed_tokens_for_user(user_id: int) -> list[ApiToken]:
    now = now_utc()
    rows = _all_user_managed_tokens_for_user(user_id)
    out: list[ApiToken] = []
    for token in rows:
        if token.revoked_at is not None:
            continue
        if token.expires_at is not None and token.expires_at <= now:
            continue
        out.append(token)
    return out


def _token_status(token: ApiToken, *, now: datetime | None = None) -> str:
    ref = now or now_utc()
    if token.revoked_at is not None:
        return "revoked"
    if token.expires_at is not None and token.expires_at <= ref:
        return "expired"
    return "active"


def _serialize_personal_token(token: ApiToken, *, now: datetime | None = None) -> dict[str, object]:
    status = _token_status(token, now=now)
    return {
        "id": int(token.id),
        "name": str(token.name or "default"),
        "prefix": str(token.token_prefix or ""),
        "scopes": list(token.scopes_json or []),
        "status": status,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "is_active": status == "active",
    }


def _serialize_revealed_token(row: dict[str, object]) -> dict[str, object]:
    expires_at = row.get("expires_at")
    created_at = row.get("created_at")
    return {
        "id": int(row.get("id", 0) or 0),
        "name": str(row.get("name") or "default"),
        "prefix": str(row.get("prefix") or ""),
        "token": str(row.get("token") or ""),
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
    }


def _api_key_workspace_payload(user_id: int, *, revealed_tokens: list[dict[str, object]] | None = None) -> dict[str, object]:
    now = now_utc()
    tokens = _all_user_managed_tokens_for_user(user_id)
    active_count = sum(1 for token in tokens if _token_status(token, now=now) == "active")
    return {
        "personal_tokens": [_serialize_personal_token(token, now=now) for token in tokens],
        "personal_token_total": len(tokens),
        "active_personal_token_total": active_count,
        "revealed_tokens": [_serialize_revealed_token(row) for row in list(revealed_tokens or [])],
        "pending_token_count": len(list(revealed_tokens or [])),
    }


def _parse_token_expiry_days(raw_value: str | None) -> datetime | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except Exception:
        raise ValueError("Expiry days must be a number.")
    if days < 1 or days > 3650:
        raise ValueError("Expiry days must be between 1 and 3650.")
    return now_utc() + timedelta(days=days)


def _parse_requested_scopes(raw_value: str | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for chunk in str(raw_value or "").split(","):
        scope = str(chunk or "").strip().lower()
        if not scope:
            continue
        if len(scope) > 64 or not re.fullmatch(r"[a-z0-9:_\-*]+", scope):
            raise ValueError(f"Invalid scope: {scope}")
        if scope not in seen:
            seen.add(scope)
            items.append(scope)
    return items or ["profile:read"]


def _create_personal_api_key(
    *,
    name: str,
    current_pwd: str,
    otp_code: str,
    recovery_code: str,
    scopes_raw: str | None,
    expiry_days_raw: str | None,
) -> tuple[ApiToken, str]:
    if not current_user.check_password(current_pwd):
        raise ValueError("Current password is invalid.")
    if bool(current_user.mfa_enabled):
        ok, _via = _verify_mfa_code(current_user, otp_code, recovery_code, consume_recovery=True)
        if not ok:
            db.session.rollback()
            raise ValueError("MFA verification failed.")
    expires_at = _parse_token_expiry_days(expiry_days_raw)
    scopes = _parse_requested_scopes(scopes_raw)
    safe_name = name or f"personal-{now_utc().strftime('%Y%m%d-%H%M%S')}"
    active_count = len(_user_managed_tokens_for_user(int(current_user.id)))
    if int(active_count or 0) >= 20:
        raise ValueError("Too many active personal API keys. Revoke one before creating another.")

    token_row, raw_token = ApiToken.create(
        user_id=int(current_user.id),
        name=safe_name,
        expires_at=expires_at,
        addon_key=None,
        created_by_user_id=int(current_user.id),
        scopes=scopes,
    )
    db.session.add(token_row)
    db.session.flush()
    reveal = ApiTokenReveal.create_encrypted(
        user_id=int(current_user.id),
        raw_token=raw_token,
        token_prefix=token_row.token_prefix,
        secret_key=str(current_app.config.get("SECRET_KEY", "")),
        name=safe_name,
        expires_at=expires_at,
        created_by_user_id=int(current_user.id),
        token_id=int(token_row.id),
    )
    db.session.add(reveal)
    db.session.commit()
    audit(
        "auth.api_key_created",
        "Personal API key created",
        level="WARNING",
        context={"uid": int(current_user.id), "token_id": int(token_row.id), "name": safe_name, "scopes": scopes},
    )
    return token_row, raw_token


def _signup_retry_cooldown_sec() -> int:
    cfg = _auth_cfg()
    return max(60, min(int(cfg.get("SIGNUP_RETRY_COOLDOWN_SEC", 900)), 86400))


def _password_policy_error(password: str) -> str | None:
    raw = str(password or "")
    if len(raw) < 8:
        return "La password deve essere di almeno 8 caratteri."
    if not re.search(r"[A-Z]", raw):
        return "La password deve contenere almeno una lettera maiuscola."
    if not re.search(r"[a-z]", raw):
        return "La password deve contenere almeno una lettera minuscola."
    if not re.search(r"\d", raw):
        return "La password deve contenere almeno un numero."
    if not re.search(r"[^A-Za-z0-9]", raw):
        return "La password deve contenere almeno un carattere speciale."
    return None


def _mfa_enabled_runtime() -> bool:
    return bool(_auth_cfg().get("MFA_ENABLED", True))


def _mfa_recovery_codes_count() -> int:
    cfg = _auth_cfg()
    return max(4, min(int(cfg.get("MFA_RECOVERY_CODES_COUNT", 10)), 20))


def _mfa_issuer() -> str:
    cfg = _auth_cfg()
    issuer = str(cfg.get("MFA_ISSUER", "")).strip()
    if issuer:
        return issuer[:64]
    return str(get_runtime_config_value("APP_NAME", "WebApp"))[:64]


def _mfa_fernet():
    return _derive_fernet_key(str(current_app.config.get("SECRET_KEY", "")), "mfa-secret-v1")


def _encrypt_mfa_secret(secret: str) -> str:
    return _mfa_fernet().encrypt(str(secret).encode("utf-8")).decode("utf-8")


def _decrypt_mfa_secret(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _mfa_fernet().decrypt(str(ciphertext).encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def _normalize_otp_code(raw: str | None) -> str:
    return re.sub(r"\D+", "", str(raw or "").strip())


def _normalize_recovery_code(raw: str | None) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip().upper())
    return clean[:64]


def _recovery_code_hash(raw_code: str) -> str:
    key = str(current_app.config.get("SECRET_KEY", "")).encode("utf-8")
    return hmac.new(key, str(raw_code).encode("utf-8"), hashlib.sha256).hexdigest()


def _generate_recovery_codes(count: int) -> list[str]:
    out: list[str] = []
    for _ in range(max(1, int(count))):
        part_a = secrets.token_hex(2).upper()
        part_b = secrets.token_hex(2).upper()
        part_c = secrets.token_hex(2).upper()
        out.append(f"{part_a}-{part_b}-{part_c}")
    return out


def _verify_mfa_code(user: User, otp_code: str | None, recovery_code: str | None, *, consume_recovery: bool) -> tuple[bool, str]:
    if not bool(user.mfa_enabled):
        return False, "mfa_disabled"
    secret = _decrypt_mfa_secret(getattr(user, "mfa_secret_enc", None))
    if not secret:
        return False, "secret_missing"
    normalized = _normalize_otp_code(otp_code)
    if normalized:
        totp = pyotp.TOTP(secret)
        if totp.verify(normalized, valid_window=1):
            user.mfa_last_used_at = now_utc()
            return True, "otp"
    rec = _normalize_recovery_code(recovery_code)
    if rec:
        hashes = list(getattr(user, "mfa_recovery_hashes", None) or [])
        rec_hash = _recovery_code_hash(rec)
        if rec_hash in hashes:
            if consume_recovery:
                user.mfa_recovery_hashes = [h for h in hashes if h != rec_hash]
                user.mfa_recovery_used_count = int(getattr(user, "mfa_recovery_used_count", 0) or 0) + 1
            user.mfa_last_used_at = now_utc()
            return True, "recovery"
    return False, "invalid_code"


def _safe_next_url(raw_next: str | None) -> str | None:
    candidate = str(raw_next or "").strip()
    if not candidate:
        return None
    decoded = unquote(candidate).strip()
    if not decoded.startswith("/"):
        return None
    if decoded.startswith("//"):
        return None
    parsed = urlparse(decoded)
    if parsed.scheme or parsed.netloc:
        return None
    return decoded


def _public_base_url() -> str:
    email_cfg = get_runtime_config_dict("EMAIL")
    explicit = str(email_cfg.get("PUBLIC_BASE_URL", "")).strip()
    if explicit:
        return explicit.rstrip("/")

    req_base = request.url_root.rstrip("/")
    cfg_base = str(get_runtime_config_value("BASE_URL", "")).strip().rstrip("/")

    def _is_local(u: str) -> bool:
        try:
            host = (urlparse(u).hostname or "").strip().lower()
        except Exception:
            host = ""
        return host in ("localhost", "127.0.0.1", "::1")

    if cfg_base and _is_local(req_base) and not _is_local(cfg_base):
        return cfg_base
    if req_base:
        return req_base
    if cfg_base:
        return cfg_base
    return "http://localhost:5000"


def _create_confirmation_token(user: User) -> str:
    rt = runtime_email_settings()
    EmailVerificationToken.query.filter_by(
        user_id=int(user.id),
        purpose="signup_confirm",
        consumed_at=None,
    ).update({"consumed_at": now_utc()}, synchronize_session=False)
    row, raw = EmailVerificationToken.create_token(
        user_id=int(user.id),
        sent_to_email=str(user.email).strip().lower(),
        ttl_minutes=int(rt.confirmation_token_ttl_min),
    )
    db.session.add(row)
    db.session.commit()
    return raw


def _mask_email(email: str | None) -> str:
    raw = str(email or "").strip()
    if not raw or "@" not in raw:
        return raw
    local, domain = raw.split("@", 1)
    if len(local) <= 2:
        local_masked = (local[:1] + "*") if local else "*"
    else:
        local_masked = local[:2] + ("*" * max(1, len(local) - 2))
    return f"{local_masked}@{domain}"


def _send_api_key_ready_email(user: User) -> None:
    """Notify the user that their API key is ready to use."""
    subject = f"{get_runtime_config_value('APP_NAME', 'WebApp')} · API key available"
    text = (
        f"Hello {user.name},\n\n"
        "your API key is available in your account settings area.\n\n"
        "If you did not request this action, contact support."
    )
    html = _render_html_email(
        title="API Key Available",
        subtitle=f"Hello {user.name}, your API key is ready.",
        content_html="<p style='margin:0 0 12px 0;'>Open account settings to view and manage your API key.</p>",
        cta_label="Open Settings",
        cta_url=f"{_public_base_url()}{url_for('auth.settings')}",
        footer_note="If you did not request this action, contact support.",
    )
    try:
        enqueue_email_job(to_email=user.email, subject=subject, text_body=text, html_body=html, requested_by_user_id=int(user.id))
    except Exception:
        send_email(to_email=user.email, subject=subject, text_body=text, html_body=html)


def _send_confirmation_email(user: User, raw_token: str) -> None:
    confirm_url = f"{_public_base_url()}{url_for('auth.confirm_email', token=raw_token)}"
    subject = f"{current_app.config.get('APP_NAME', 'WebApp')} · Confirm registration"
    text = (
        f"Hello {user.name},\n\n"
        "to complete your registration, confirm your email address:\n"
        f"{confirm_url}\n\n"
        "If you did not request this registration, ignore this email."
    )
    html = _render_html_email(
        title="Confirm Registration",
        subtitle=f"Hello {escape(user.name)}, complete activation of your account.",
        content_html=(
            "<p style='margin:0 0 12px 0;'>To complete your registration, confirm your email address.</p>"
            "<p style='margin:0 0 12px 0;color:#64748b;'>The link expires automatically according to the active security settings.</p>"
        ),
        cta_label="Confirm Email",
        cta_url=confirm_url,
        footer_note="If you did not request this registration, ignore this email.",
    )
    try:
        enqueue_email_job(to_email=user.email, subject=subject, text_body=text, html_body=html, requested_by_user_id=int(user.id))
    except Exception:
        send_email(to_email=user.email, subject=subject, text_body=text, html_body=html)




def _render_html_email(
    *,
    title: str,
    subtitle: str,
    content_html: str,
    cta_label: str,
    cta_url: str,
    footer_note: str,
) -> str:
    theme = get_runtime_config_dict("THEME")
    brand = str(theme.get("brand_color", "#2563eb")).strip() or "#2563eb"
    card_bg = str(theme.get("card_bg", "#ffffff")).strip() or "#ffffff"
    body_bg = str(theme.get("body_bg", "#f0f4f8")).strip() or "#f0f4f8"
    text_color = str(theme.get("text_color", "#1e293b")).strip() or "#1e293b"
    muted = str(theme.get("text_muted", "#64748b")).strip() or "#64748b"
    app_name = escape(str(get_runtime_config_value("APP_NAME", "WebApp")))
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'></head>"
        f"<body style='margin:0;padding:24px;background:{body_bg};font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica Neue,Arial,sans-serif;color:{text_color};'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='max-width:660px;margin:0 auto;'>"
        "<tr><td>"
        f"<div style='padding:0 0 14px 4px;font-size:14px;font-weight:700;color:{brand};'>{app_name}</div>"
        f"<div style='background:{card_bg};border:1px solid #e5eaf0;border-radius:14px;overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,.08);'>"
        f"<div style='height:6px;background:{brand};'></div>"
        "<div style='padding:24px;'>"
        f"<h1 style='margin:0 0 8px 0;font-size:22px;line-height:1.2;color:{text_color};'>{escape(title)}</h1>"
        f"<p style='margin:0 0 18px 0;font-size:14px;color:{muted};'>{subtitle}</p>"
        f"{content_html}"
        "<div style='margin:20px 0 12px 0;'>"
        f"<a href='{escape(cta_url)}' style='display:inline-block;background:{brand};color:#ffffff;text-decoration:none;font-weight:600;padding:10px 16px;border-radius:10px;font-size:14px;'>{escape(cta_label)}</a>"
        "</div>"
        f"<p style='margin:0;font-size:12px;color:{muted};'>{escape(footer_note)}</p>"
        "</div>"
        "</div>"
        "<p style='margin:12px 4px 0 4px;font-size:11px;color:#94a3b8;'>Automated message. Do not reply to this email.</p>"
        "</td></tr></table></body></html>"
    )


def _revoke_browser_session_token(uid: int | None) -> None:
    if not uid:
        return
    token_id = session.get("browser_token_id")
    if not token_id:
        return
    tok = ApiToken.query.filter_by(id=int(token_id), user_id=int(uid), name="browser-session").first()
    if tok and tok.revoked_at is None:
        tok.revoked_at = now_utc()
        audit(
            "auth.browser_session_token_revoked",
            "Browser session token revoked on logout",
            context={"uid": int(uid), "token_id": int(tok.id)},
        )


def _finalize_login(user: User, *, remember: bool, next_url: str | None) -> object:
    session.clear()
    login_user(user, remember=bool(remember), fresh=True)
    user.last_login_at = now_utc()
    user.last_seen_at = now_utc()
    user.last_ip = _client_ip()
    user.failed_login_count = 0
    user.locked_until = None
    _issue_browser_session(user)
    db.session.commit()
    if (
        _send_api_key_notice_on_signin_enabled()
        and bool(user.notification_email_enabled)
        and int(ApiTokenReveal.query.filter_by(user_id=user.id, revealed_at=None).count()) > 0
    ):
        try:
            _send_api_key_ready_email(user)
            audit(
                "auth.api_key_notice_sent_on_signin",
                "API key ready email sent on sign-in",
                context={"uid": user.id, "email": user.email},
            )
        except EmailServiceError as ex:
            audit(
                "auth.api_key_notice_signin_failed",
                "API key ready email failed on sign-in",
                context={"uid": user.id, "email": user.email, "error": str(ex)[:180]},
                level="WARNING",
            )
    audit(
        "auth.login_success",
        f"Login riuscito per {user.email}",
        context={"email": user.email, "role": user.role, "uid": user.id, "user_agent": _client_user_agent(), "ip": _client_ip()},
    )
    flash(f"Bentornato, {user.name}!", "success")
    if next_url:
        return redirect(next_url)
    if user.role == "admin":
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("main.dashboard"))


@bp.get("/login")
def login():
    force = request.args.get("force") in ("1", "true", "yes")
    if current_user.is_authenticated and not force:
        return redirect(url_for("main.dashboard"))
    form = LoginForm()
    features = get_runtime_feature_flags()
    return render_template(
        "auth/login.html",
        form=form,
        remember_enabled=features.get("remember_me", True),
        self_registration_enabled=_self_registration_enabled(),
        pending_confirmation_email="",
    )


@bp.post("/login")
@limiter.limit(lambda: str(__import__('flask').current_app.config.get('SECURITY', {}).get('LOGIN_RATE_LIMIT', '5 per minute')))
def login_post():
    form = LoginForm()
    features = get_runtime_feature_flags()
    remember_enabled = features.get("remember_me", True)
    if not form.validate_on_submit():
        flash(translate("auth.invalid_input", "Invalid input."), "danger")
        return render_template(
            "auth/login.html",
            form=form,
            remember_enabled=remember_enabled,
            self_registration_enabled=_self_registration_enabled(),
            pending_confirmation_email="",
        ), 400

    email = form.email.data.strip().lower()
    max_attempts, lock_minutes = _failed_login_limits()
    # If another tab already authenticated a different user, allow explicit account switch.
    if current_user.is_authenticated and current_user.email != email:
        old_uid = int(getattr(current_user, "id", 0) or 0)
        audit(
            "auth.user_switch",
            f"Switch account {current_user.email} -> {email}",
            level="WARNING",
            context={"from": current_user.email, "to": email},
        )
        _revoke_browser_session_row(old_uid)
        _revoke_browser_session_token(old_uid)
        db.session.commit()
        logout_user()
        session.clear()
    elif current_user.is_authenticated and current_user.email == email:
        return redirect(url_for("admin.dashboard" if current_user.role == "admin" else "main.dashboard"))

    user = User.query.filter_by(email=email).first()

    if user and user.locked_until and user.locked_until > now_utc():
        flash(translate("auth.account_locked", "Account temporarily locked. Try again later."), "danger")
        audit(
            "auth.login_locked",
            "Login blocked due to temporary lock",
            level="WARNING",
            context={"email": email, "uid": user.id, "locked_until": user.locked_until.isoformat()},
        )
        return render_template(
            "auth/login.html",
            form=form,
            remember_enabled=remember_enabled,
            self_registration_enabled=_self_registration_enabled(),
            pending_confirmation_email="",
        ), 423

    if not user or not user.is_active or not user.check_password(form.password.data):
        time.sleep(0.35)
        if user:
            user.failed_login_count = int(user.failed_login_count or 0) + 1
            if user.failed_login_count >= max_attempts:
                user.locked_until = now_utc() + timedelta(minutes=lock_minutes)
                user.failed_login_count = 0
            db.session.commit()
        reason = "user_not_found" if not user else ("inactive" if not user.is_active else "bad_password")
        audit(
            "auth.login_failed",
            f"Login failed for {email}",
            level="WARNING",
            context={"email": email, "reason": reason, "uid": int(user.id) if user else None, "user_agent": _client_user_agent(), "ip": _client_ip()},
        )
        flash(translate("auth.invalid_credentials", "Invalid email or password."), "danger")
        return render_template(
            "auth/login.html",
            form=form,
            remember_enabled=remember_enabled,
            self_registration_enabled=_self_registration_enabled(),
            pending_confirmation_email="",
        ), 401

    if _is_pending_email_confirmation(user):
        flash(translate("auth.confirm_email_before_login", "Confirm your email before signing in."), "warning")
        return render_template(
            "auth/login.html",
            form=form,
            remember_enabled=remember_enabled,
            self_registration_enabled=_self_registration_enabled(),
            pending_confirmation_email=user.email,
        ), 403

    remember_selected = bool(form.remember.data) and remember_enabled
    next_url = _safe_next_url(request.args.get("next"))
    if _mfa_enabled_runtime() and bool(user.mfa_enabled):
        session.clear()
        # TODO: move MFA attempt counters to shared state (DB/Redis) for multi-worker atomicity.
        session["mfa_pending"] = {
            "uid": int(user.id),
            "remember": bool(remember_selected),
            "next_url": str(next_url or ""),
            "issued_at": int(time.time()),
            "attempts": 0,
        }
        audit(
            "auth.mfa_challenge_required",
            "MFA challenge required",
            context={"uid": user.id, "email": user.email},
        )
        return redirect(url_for("auth.mfa_challenge"))

    return _finalize_login(user, remember=remember_selected, next_url=next_url)


@bp.route("/mfa/challenge", methods=["GET", "POST"])
@limiter.limit("20 per 10 minute")
def mfa_challenge():
    pending = dict(session.get("mfa_pending") or {})
    if not pending:
        flash(translate("auth.invalid_mfa_session", "Invalid MFA session. Please sign in again."), "warning")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, int(pending.get("uid", 0) or 0))
    if not user or not user.is_active or not bool(user.mfa_enabled):
        session.pop("mfa_pending", None)
        flash(translate("auth.invalid_mfa_session", "Invalid MFA session. Please sign in again."), "warning")
        return redirect(url_for("auth.login"))
    issued_at = int(pending.get("issued_at", 0) or 0)
    if issued_at <= 0 or (int(time.time()) - issued_at) > 600:
        session.pop("mfa_pending", None)
        flash(translate("auth.expired_mfa_session", "MFA session expired. Please sign in again."), "warning")
        audit(
            "auth.mfa_challenge_expired",
            "MFA challenge expired",
            level="WARNING",
            context={"uid": user.id, "email": user.email},
        )
        return redirect(url_for("auth.login"))

    form = MfaVerifyForm()
    if request.method == "POST":
        if not form.validate_on_submit():
            flash(translate("auth.enter_valid_mfa", "Enter a valid MFA code."), "danger")
            return render_template("auth/mfa_challenge.html", form=form, masked_email=user.email)
        ok, via = _verify_mfa_code(user, form.otp_code.data, form.recovery_code.data, consume_recovery=True)
        if not ok:
            pending["attempts"] = int(pending.get("attempts", 0) or 0) + 1
            session["mfa_pending"] = pending
            session.modified = True
            delay = min(2.5, 0.25 * int(pending["attempts"]))
            time.sleep(delay)
            audit(
                "auth.mfa_challenge_failed",
                "MFA challenge failed",
                level="WARNING",
                context={"uid": user.id, "email": user.email, "attempt": int(pending["attempts"]), "delay_sec": delay},
            )
            if int(pending["attempts"]) >= 5:
                audit(
                    "auth.mfa_challenge_anomalous_attempts",
                    "High MFA failure count detected",
                    level="WARNING",
                    context={"uid": user.id, "email": user.email, "attempt": int(pending["attempts"])},
                )
            if int(pending["attempts"]) >= 10:
                session.pop("mfa_pending", None)
                flash(translate("auth.too_many_mfa_attempts", "Too many failed MFA attempts. Please sign in again."), "danger")
                return redirect(url_for("auth.login"))
            flash(translate("auth.invalid_mfa_code", "Invalid MFA code."), "danger")
            return render_template("auth/mfa_challenge.html", form=form, masked_email=user.email)
        db.session.commit()
        session.pop("mfa_pending", None)
        audit(
            "auth.mfa_challenge_success",
            "MFA challenge passed",
            context={"uid": user.id, "email": user.email, "via": via},
        )
        return _finalize_login(
            user,
            remember=bool(pending.get("remember", False)),
            next_url=_safe_next_url(str(pending.get("next_url", "") or "")),
        )
    return render_template("auth/mfa_challenge.html", form=form, masked_email=user.email)


@bp.get("/register")
def register():
    if not _self_registration_enabled():
        flash(translate("auth.self_registration_disabled", "Self-service registration is disabled."), "warning")
        return redirect(url_for("auth.login"))
    form = RegistrationForm()
    return render_template("auth/register.html", form=form)


@bp.post("/register")
@limiter.limit("20 per hour")
def register_post():
    if not _self_registration_enabled():
        flash(translate("auth.self_registration_disabled", "Self-service registration is disabled."), "warning")
        return redirect(url_for("auth.login"))

    form = RegistrationForm()
    if not form.validate_on_submit():
        errors = []
        for field, messages in form.errors.items():
            label = getattr(getattr(form, field, None), "label", None)
            field_name = str(getattr(label, "text", field))
            for msg in messages:
                errors.append(f"{field_name}: {msg}")
        if errors:
            flash(translate("auth.check_input_with_details", "Check the submitted data: ") + " | ".join(errors), "danger")
        else:
            flash(translate("auth.check_input", "Check the submitted data."), "danger")
        return render_template("auth/register.html", form=form), 400

    pwd_error = _password_policy_error(form.password.data or "")
    if pwd_error:
        flash(pwd_error, "danger")
        return render_template("auth/register.html", form=form), 400

    email = form.email.data.strip().lower()
    existing = User.query.filter_by(email=email).first()
    generic_msg = translate("auth.generic_signup_notice", "If the data is valid, you will receive an email with the next steps.")
    if existing:
        if existing.email_verified:
            audit(
                "auth.register_existing_email",
                "Registration attempted on existing verified email",
                context={"email": email},
                level="WARNING",
            )
            flash(generic_msg, "info")
            return redirect(url_for("auth.login"))
        latest_pending = (
            EmailVerificationToken.query.filter_by(
                user_id=existing.id,
                purpose="signup_confirm",
                consumed_at=None,
            )
            .with_for_update()
            .order_by(EmailVerificationToken.created_at.desc())
            .first()
        )
        cooldown_sec = _signup_retry_cooldown_sec()
        if latest_pending and latest_pending.created_at:
            elapsed = (now_utc() - latest_pending.created_at).total_seconds()
            if elapsed < cooldown_sec:
                audit(
                    "auth.register_retry_cooldown",
                    "Signup retry blocked by cooldown",
                    context={"uid": existing.id, "email": existing.email, "wait_sec": int(cooldown_sec - elapsed)},
                    level="WARNING",
                )
                flash(generic_msg, "info")
                return redirect(url_for("auth.login"))
        user = existing
        user.is_active = False
        user.account_status = "pending_verification"
        user.signup_source = user.signup_source or "self_signup"
    else:
        user = User(
            email=email,
            name=form.name.data.strip()[:120],
            role="user",
            is_active=False,
            email_verified=False,
            account_status="pending_verification",
            signup_source="self_signup",
            terms_accepted_at=now_utc(),
            privacy_accepted_at=now_utc(),
            marketing_consent=False,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()

    raw = _create_confirmation_token(user)
    try:
        _send_confirmation_email(user, raw)
        audit(
            "auth.registered",
            "New self-registration created",
            context={"uid": user.id, "email": user.email},
            level="WARNING",
        )
        flash(generic_msg, "success")
    except EmailServiceError as ex:
        audit(
            "auth.register_email_failed",
            "Registration confirmation email failed",
            context={"uid": user.id, "email": user.email, "error": str(ex)[:180]},
            level="ERROR",
        )
        flash(translate("auth.signup_created_email_failed", "Registration was created, but the email could not be sent. Contact an admin."), "warning")
    return redirect(url_for("auth.register_pending", email=_mask_email(user.email)))


@bp.get("/register/pending")
def register_pending():
    if not _self_registration_enabled():
        return redirect(url_for("auth.login"))
    return render_template(
        "auth/register_pending.html",
        masked_email=str(request.args.get("email") or "").strip(),
    )


@bp.get("/confirm-email/<token>")
def confirm_email(token: str):
    token_hash = EmailVerificationToken.hash_token(str(token or ""))
    row = EmailVerificationToken.query.filter_by(token_hash=token_hash, purpose="signup_confirm").first()
    if not row or not row.is_valid():
        flash(translate("auth.invalid_confirmation_link", "Confirmation link is invalid or expired."), "danger")
        return redirect(url_for("auth.login"))

    user = db.session.get(User, int(row.user_id))
    if not user:
        flash(translate("auth.user_not_found", "User not found."), "danger")
        return redirect(url_for("auth.login"))
    if str(row.sent_to_email or "").strip().lower() != str(user.email or "").strip().lower():
        row.consumed_at = now_utc()
        db.session.commit()
        flash(translate("auth.invalid_confirmation_link", "Confirmation link is invalid or expired."), "danger")
        return redirect(url_for("auth.login"))

    now = now_utc()
    row.consumed_at = now
    if user.email_verified:
        db.session.commit()
        flash(translate("auth.email_already_confirmed", "Email was already confirmed earlier."), "info")
        return redirect(url_for("auth.login"))

    user.email_verified = True
    user.email_verified_at = now
    user.is_active = True
    user.account_status = "active"
    user.last_seen_at = now
    EmailVerificationToken.query.filter(
        EmailVerificationToken.user_id == int(user.id),
        EmailVerificationToken.purpose == "signup_confirm",
        EmailVerificationToken.consumed_at.is_(None),
        EmailVerificationToken.id != int(row.id),
    ).update({"consumed_at": now}, synchronize_session=False)

    audit(
        "auth.email_confirmed",
        "Email confirmation completed",
        context={"uid": user.id, "email": user.email},
        level="WARNING",
    )
    db.session.commit()
    flash("Email confirmed. Account activated.", "success")
    return _finalize_login(user, remember=False, next_url=None)


@bp.post("/resend-confirmation")
@limiter.limit("15 per hour")
def resend_confirmation():
    if not _self_registration_enabled():
        return redirect(url_for("auth.login"))

    email = str(request.form.get("email") or "").strip().lower()
    if not email:
        flash(translate("auth.enter_valid_email", "Enter a valid email address."), "warning")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(email=email).first()
    if user and _is_pending_email_confirmation(user):
        latest_pending = (
            EmailVerificationToken.query.filter_by(
                user_id=user.id,
                purpose="signup_confirm",
                consumed_at=None,
            )
            .order_by(EmailVerificationToken.created_at.desc())
            .first()
        )
        cooldown_sec = _signup_retry_cooldown_sec()
        if latest_pending and latest_pending.created_at:
            elapsed = (now_utc() - latest_pending.created_at).total_seconds()
            if elapsed < cooldown_sec:
                flash(translate("auth.resend_if_pending", "If the account exists and is pending, the confirmation email was re-sent."), "info")
                return redirect(url_for("auth.login"))
        raw = _create_confirmation_token(user)
        try:
            _send_confirmation_email(user, raw)
            audit(
                "auth.confirmation_resent",
                "Confirmation email resent",
                context={"uid": user.id, "email": user.email},
            )
        except EmailServiceError as ex:
            audit(
                "auth.confirmation_resend_failed",
                "Failed to resend confirmation email",
                context={"uid": user.id, "email": user.email, "error": str(ex)[:180]},
                level="ERROR",
            )
    flash(translate("auth.resend_if_pending", "If the account exists and is pending, the confirmation email was re-sent."), "info")
    return redirect(url_for("auth.login"))


@bp.get("/logout")
@login_required
def logout():
    uid = int(getattr(current_user, "id", 0) or 0)
    try:
        _revoke_browser_session_row(uid)
        _revoke_browser_session_token(uid)
        db.session.commit()
    except Exception as ex:
        db.session.rollback()
        audit(
            "auth.browser_session_token_revoke_failed",
            "Browser session token revoke failed on logout",
            level="WARNING",
            context={"uid": uid, "error": str(ex)[:180]},
        )
    audit("auth.logout", "Logout")
    logout_user()
    session.clear()
    flash(translate("auth.logged_out_successfully", "Signed out successfully."), "info")
    return redirect(url_for("auth.login"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = UserSettingsForm(
        name=current_user.name,
        username=current_user.username or "",
        locale=current_user.locale or ("it-IT" if session.get("ui_lang") == "it" else "en"),
        timezone=current_user.timezone or "",
        notes=current_user.notes or "",
        notification_email_enabled=bool(current_user.notification_email_enabled),
        notification_security_enabled=bool(current_user.notification_security_enabled),
    )
    mfa_setup = dict(session.get("mfa_setup") or {})
    mfa_setup_secret = str(mfa_setup.get("secret", "") or "")
    mfa_setup_expires_in = 0
    mfa_setup_uri = ""
    if mfa_setup_secret:
        created_at = int(mfa_setup.get("created_at", 0) or 0)
        mfa_setup_expires_in = max(0, 600 - max(0, int(time.time()) - created_at))
        if mfa_setup_expires_in <= 0:
            session.pop("mfa_setup", None)
            mfa_setup_secret = ""
        else:
            mfa_setup_uri = pyotp.totp.TOTP(mfa_setup_secret).provisioning_uri(
                name=str(current_user.email),
                issuer_name=_mfa_issuer(),
            )
    mfa_recovery_codes_plain = list(session.pop("mfa_recovery_codes_plain", []) or [])
    revealed_tokens = _rehydrate_pending_token_reveals(int(current_user.id))
    api_key_workspace = _api_key_workspace_payload(int(current_user.id), revealed_tokens=revealed_tokens)
    if form.validate_on_submit():
        new_username = (form.username.data or "").strip().lower()[:80]
        if new_username:
            existing = User.query.filter(User.username == new_username, User.id != current_user.id).first()
            if existing:
                flash(translate("auth.username_already_taken", "Username already used by another account."), "danger")
                return redirect(url_for("auth.settings"))
        current_user.name = form.name.data.strip()
        current_user.username = new_username or None
        current_user.locale = (form.locale.data or "").strip()[:16] or None
        session["ui_lang"] = normalize_language(current_user.locale or session.get("ui_lang"))
        current_user.timezone = (form.timezone.data or "").strip()[:64] or None
        current_user.notes = (form.notes.data or "").strip()[:4000] or None
        current_user.notification_email_enabled = bool(form.notification_email_enabled.data)
        current_user.notification_security_enabled = bool(form.notification_security_enabled.data)
        db.session.commit()
        audit(
            "user.settings_updated",
            translate("auth.settings_updated_audit", "Account settings updated"),
            context={"name": current_user.name, "uid": current_user.id, "username": current_user.username or ""},
        )
        flash(translate("common.changes_saved", "Changes saved."), "success")
        return redirect(url_for("auth.settings"))

    return render_template(
        "auth/settings.html",
        form=form,
        mfa_enabled=bool(current_user.mfa_enabled),
        mfa_recovery_remaining=len(list(current_user.mfa_recovery_hashes or [])),
        mfa_last_used_at=current_user.mfa_last_used_at,
        mfa_setup_secret=mfa_setup_secret,
        mfa_setup_uri=mfa_setup_uri,
        mfa_setup_expires_in=mfa_setup_expires_in,
        mfa_recovery_codes_plain=mfa_recovery_codes_plain,
        **api_key_workspace,
        now_utc=now_utc(),
    )


@bp.post("/change-password")
@login_required
@limiter.limit("5 per minute")
def change_password():
    current_pwd = request.form.get("current_password", "")
    new_pwd = request.form.get("new_password", "")
    confirm_pwd = request.form.get("confirm_password", "")

    if not current_user.check_password(current_pwd):
        time.sleep(1.0)
        flash("Password attuale errata.", "danger")
        return redirect(url_for("auth.settings"))
    pwd_error = _password_policy_error(new_pwd)
    if pwd_error:
        flash(pwd_error, "danger")
        return redirect(url_for("auth.settings"))
    if new_pwd != confirm_pwd:
        flash("Le password non coincidono.", "danger")
        return redirect(url_for("auth.settings"))

    current_user.set_password(new_pwd)
    current_user.last_password_change_at = now_utc()
    UserSession.query.filter(
        UserSession.user_id == int(current_user.id),
        UserSession.revoked_at.is_(None),
    ).update({"revoked_at": now_utc(), "status": "revoked"}, synchronize_session=False)
    ApiToken.query.filter(
        ApiToken.user_id == int(current_user.id),
        ApiToken.name == "browser-session",
        ApiToken.revoked_at.is_(None),
    ).update({"revoked_at": now_utc()}, synchronize_session=False)
    db.session.commit()
    audit("auth.password_changed", "Password cambiata")
    logout_user()
    session.clear()
    flash("Password updated. Sign in again with the new credentials.", "success")
    return redirect(url_for("auth.login"))


@bp.post("/mfa/setup")
@login_required
@limiter.limit("5 per minute")
@limiter.limit("10 per hour")
def mfa_setup():
    if not _mfa_enabled_runtime():
        flash("MFA disabilitata a livello applicativo.", "warning")
        return redirect(url_for("auth.settings"))
    current_pwd = str(request.form.get("current_password", "") or "")
    if not current_user.check_password(current_pwd):
        flash("Password attuale errata.", "danger")
        return redirect(url_for("auth.settings"))
    secret = pyotp.random_base32()
    session["mfa_setup"] = {"secret": secret, "created_at": int(time.time())}
    session.modified = True
    audit(
        "auth.mfa_setup_started",
        "User started MFA setup",
        context={"uid": current_user.id, "email": current_user.email},
    )
    flash("Setup MFA avviato. Configura l'app autenticatore e conferma con un codice.", "info")
    return redirect(url_for("auth.settings"))


@bp.post("/mfa/enable")
@login_required
@limiter.limit("20 per hour")
def mfa_enable():
    if not _mfa_enabled_runtime():
        flash("MFA disabilitata a livello applicativo.", "warning")
        return redirect(url_for("auth.settings"))
    setup = dict(session.get("mfa_setup") or {})
    secret = str(setup.get("secret", "") or "")
    created_at = int(setup.get("created_at", 0) or 0)
    if not secret or created_at <= 0 or (int(time.time()) - created_at) > 600:
        session.pop("mfa_setup", None)
        flash("Setup MFA scaduto. Avvia di nuovo la configurazione.", "warning")
        return redirect(url_for("auth.settings"))
    otp_code = str(request.form.get("otp_code", "") or "")
    totp = pyotp.TOTP(secret)
    if not totp.verify(_normalize_otp_code(otp_code), valid_window=1):
        time.sleep(0.2)
        flash("Codice autenticatore non valido.", "danger")
        return redirect(url_for("auth.settings"))
    recovery_codes = _generate_recovery_codes(_mfa_recovery_codes_count())
    current_user.mfa_secret_enc = _encrypt_mfa_secret(secret)
    current_user.mfa_enabled = True
    current_user.mfa_enrolled_at = now_utc()
    current_user.mfa_last_used_at = now_utc()
    current_user.mfa_recovery_hashes = [_recovery_code_hash(_normalize_recovery_code(c)) for c in recovery_codes]
    current_user.mfa_recovery_used_count = 0
    db.session.commit()
    session.pop("mfa_setup", None)
    session["mfa_recovery_codes_plain"] = recovery_codes
    session.modified = True
    audit(
        "auth.mfa_enabled",
        "MFA enabled",
        level="WARNING",
        context={"uid": current_user.id, "email": current_user.email},
    )
    flash("MFA attivata. Salva subito i recovery code.", "success")
    return redirect(url_for("auth.settings"))


@bp.post("/mfa/disable")
@login_required
@limiter.limit("10 per hour")
def mfa_disable():
    if not bool(current_user.mfa_enabled):
        flash("MFA non attiva.", "info")
        return redirect(url_for("auth.settings"))
    current_pwd = str(request.form.get("current_password", "") or "")
    if not current_user.check_password(current_pwd):
        flash("Password attuale errata.", "danger")
        return redirect(url_for("auth.settings"))
    ok, _via = _verify_mfa_code(
        current_user,
        request.form.get("otp_code"),
        request.form.get("recovery_code"),
        consume_recovery=True,
    )
    if not ok:
        time.sleep(0.2)
        flash("Codice MFA non valido.", "danger")
        return redirect(url_for("auth.settings"))
    current_user.mfa_enabled = False
    current_user.mfa_secret_enc = None
    current_user.mfa_recovery_hashes = []
    current_user.mfa_enrolled_at = None
    current_user.mfa_last_used_at = None
    db.session.commit()
    session.pop("mfa_setup", None)
    session.pop("mfa_recovery_codes_plain", None)
    audit(
        "auth.mfa_disabled",
        "MFA disabled",
        level="WARNING",
        context={"uid": current_user.id, "email": current_user.email},
    )
    flash("MFA disattivata.", "warning")
    return redirect(url_for("auth.settings"))


@bp.post("/mfa/recovery/regenerate")
@login_required
@limiter.limit("10 per hour")
def mfa_regenerate_recovery():
    if not bool(current_user.mfa_enabled):
        flash("Attiva MFA prima di rigenerare i recovery code.", "warning")
        return redirect(url_for("auth.settings"))
    current_pwd = str(request.form.get("current_password", "") or "")
    if not current_user.check_password(current_pwd):
        flash("Password attuale errata.", "danger")
        return redirect(url_for("auth.settings"))
    ok, _via = _verify_mfa_code(
        current_user,
        request.form.get("otp_code"),
        request.form.get("recovery_code"),
        consume_recovery=True,
    )
    if not ok:
        flash("Codice MFA non valido.", "danger")
        return redirect(url_for("auth.settings"))
    recovery_codes = _generate_recovery_codes(_mfa_recovery_codes_count())
    current_user.mfa_recovery_hashes = [_recovery_code_hash(_normalize_recovery_code(c)) for c in recovery_codes]
    current_user.mfa_recovery_used_count = 0
    db.session.commit()
    session["mfa_recovery_codes_plain"] = recovery_codes
    session.modified = True
    audit(
        "auth.mfa_recovery_regenerated",
        "MFA recovery codes regenerated",
        level="WARNING",
        context={"uid": current_user.id, "email": current_user.email},
    )
    flash("Recovery code rigenerati.", "success")
    return redirect(url_for("auth.settings"))


@bp.post("/api-keys/create")
@login_required
@limiter.limit("20 per hour")
def api_keys_create():
    name = str(request.form.get("name") or "").strip()[:80]
    current_pwd = str(request.form.get("current_password") or "")
    otp_code = str(request.form.get("otp_code") or "")
    recovery_code = str(request.form.get("recovery_code") or "")
    scopes_raw = request.form.get("scopes")
    expiry_days_raw = request.form.get("expiry_days")

    try:
        _create_personal_api_key(
            name=name,
            current_pwd=current_pwd,
            otp_code=otp_code,
            recovery_code=recovery_code,
            scopes_raw=scopes_raw,
            expiry_days_raw=expiry_days_raw,
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("auth.settings"))
    flash("Personal API key created. It is shown only once in the account page.", "success")
    return redirect(url_for("auth.settings"))


@bp.post("/api-keys/<int:token_id>/revoke")
@login_required
@limiter.limit("60 per hour")
def api_keys_revoke(token_id: int):
    token = ApiToken.query.filter_by(id=int(token_id), user_id=int(current_user.id)).first()
    if token is None or str(token.name or "") == "browser-session":
        flash("API key not found.", "warning")
        return redirect(url_for("auth.settings"))
    if token.revoked_at is None:
        token.revoked_at = now_utc()
        db.session.commit()
        audit(
            "auth.api_key_revoked",
            "Personal API key revoked",
            level="WARNING",
            context={"uid": int(current_user.id), "token_id": int(token.id), "name": token.name},
        )
        flash("API key revoked.", "success")
    else:
        flash("API key was already revoked.", "info")
    return redirect(url_for("auth.settings"))


@bp.get("/api-keys/data")
@login_required
def api_keys_data():
    revealed_tokens = _rehydrate_pending_token_reveals(int(current_user.id))
    return jsonify({"ok": True, **_api_key_workspace_payload(int(current_user.id), revealed_tokens=revealed_tokens)})


@bp.post("/api-keys/create.json")
@login_required
@limiter.limit("20 per hour")
def api_keys_create_json():
    data = request.get_json(silent=True) or {}
    try:
        token_row, raw_token = _create_personal_api_key(
            name=str(data.get("name") or "").strip()[:80],
            current_pwd=str(data.get("current_password") or ""),
            otp_code=str(data.get("otp_code") or ""),
            recovery_code=str(data.get("recovery_code") or ""),
            scopes_raw=data.get("scopes"),
            expiry_days_raw=data.get("expiry_days"),
        )
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    payload = _api_key_workspace_payload(
        int(current_user.id),
        revealed_tokens=[
            {
                "id": int(token_row.id),
                "name": token_row.name,
                "prefix": token_row.token_prefix,
                "token": raw_token,
                "expires_at": token_row.expires_at,
                "created_at": token_row.created_at,
            }
        ],
    )
    return jsonify({"ok": True, **payload})


@bp.post("/api-keys/<int:token_id>/revoke.json")
@login_required
@limiter.limit("60 per hour")
def api_keys_revoke_json(token_id: int):
    token = ApiToken.query.filter_by(id=int(token_id), user_id=int(current_user.id)).first()
    if token is None or str(token.name or "") == "browser-session":
        return jsonify({"ok": False, "error": "API key not found."}), 404
    if token.revoked_at is None:
        token.revoked_at = now_utc()
        db.session.commit()
        audit(
            "auth.api_key_revoked",
            "Personal API key revoked",
            level="WARNING",
            context={"uid": int(current_user.id), "token_id": int(token.id), "name": token.name},
        )
    return jsonify({"ok": True, **_api_key_workspace_payload(int(current_user.id))})
