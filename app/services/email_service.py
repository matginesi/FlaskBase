from __future__ import annotations

import json
import os
import smtplib
import socket
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
import shutil
from typing import Any

from flask import current_app

from ..utils import get_runtime_config_value


@dataclass
class RuntimeEmailSettings:
    enabled: bool
    mode: str
    sendmail_path: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    smtp_use_ssl: bool
    smtp_timeout_sec: int
    from_email: str
    from_name: str
    reply_to: str
    config_path: str
    confirmation_token_ttl_min: int
    allow_broadcast_email: bool
    send_api_key_on_confirmation: bool


@dataclass
class ProviderEmailConfig:
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    from_email: str
    from_name: str
    reply_to: str
    use_tls: bool
    use_ssl: bool
    timeout_sec: int


class EmailServiceError(RuntimeError):
    pass


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        out = int(str(value).strip())
    except Exception:
        out = int(default)
    return max(min_value, min(max_value, out))


def runtime_email_settings() -> RuntimeEmailSettings:
    cfg = dict(current_app.config.get("EMAIL", {}) or {})
    mode = str(cfg.get("MODE", "sendmail")).strip().lower()
    if mode not in ("sendmail", "smtp", "disabled"):
        mode = "sendmail"
    hostname = (socket.getfqdn() or "localhost").strip().lower()
    default_from = f"noreply@{hostname}" if "." in hostname else "noreply@localhost"
    from_email = str(cfg.get("FROM_EMAIL", default_from)).strip().lower() or default_from
    reply_to = str(cfg.get("REPLY_TO", from_email)).strip().lower() or from_email
    smtp_host = str(cfg.get("SMTP_HOST", os.getenv("SMTP_HOST", ""))).strip()
    smtp_username = str(cfg.get("SMTP_USERNAME", os.getenv("SMTP_USERNAME", ""))).strip()
    smtp_password = str(cfg.get("SMTP_PASSWORD", os.getenv("SMTP_PASSWORD", ""))).strip()
    return RuntimeEmailSettings(
        enabled=bool(mode != "disabled") and _to_bool(cfg.get("ENABLED", True), True),
        mode=mode,
        sendmail_path=str(cfg.get("SENDMAIL_PATH", "")).strip(),
        smtp_host=smtp_host,
        smtp_port=_to_int(cfg.get("SMTP_PORT", os.getenv("SMTP_PORT", 587)), 587, 1, 65535),
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_use_tls=_to_bool(cfg.get("SMTP_USE_TLS", os.getenv("SMTP_USE_TLS", True)), True),
        smtp_use_ssl=_to_bool(cfg.get("SMTP_USE_SSL", os.getenv("SMTP_USE_SSL", False)), False),
        smtp_timeout_sec=_to_int(cfg.get("SMTP_TIMEOUT_SEC", os.getenv("SMTP_TIMEOUT_SEC", 15)), 15, 3, 120),
        from_email=from_email,
        from_name=str(cfg.get("FROM_NAME", get_runtime_config_value("APP_NAME", "WebApp"))).strip() or "WebApp",
        reply_to=reply_to,
        config_path=str(cfg.get("CONFIG_PATH", os.getenv("EMAIL_CONFIG_PATH", ""))).strip(),
        confirmation_token_ttl_min=_to_int(cfg.get("CONFIRMATION_TOKEN_TTL_MIN", 60), 60, 5, 1440),
        allow_broadcast_email=_to_bool(cfg.get("ALLOW_BROADCAST_EMAIL", True), True),
        send_api_key_on_confirmation=_to_bool(cfg.get("SEND_API_KEY_ON_CONFIRMATION", False), False),
    )


def _load_provider_config(path_str: str) -> ProviderEmailConfig:
    path = Path(path_str).expanduser()
    if not path.exists() or not path.is_file():
        raise EmailServiceError(f"email config file non trovato: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as ex:
        raise EmailServiceError(f"email config json non valido: {ex}") from ex
    if not isinstance(payload, dict):
        raise EmailServiceError("email config deve essere un oggetto JSON")

    host = str(payload.get("smtp_host", "")).strip()
    if not host:
        raise EmailServiceError("smtp_host mancante")
    from_email = str(payload.get("from_email", "")).strip().lower()
    if not from_email or "@" not in from_email:
        raise EmailServiceError("from_email non valido")

    return ProviderEmailConfig(
        smtp_host=host,
        smtp_port=_to_int(payload.get("smtp_port", 587), 587, 1, 65535),
        username=str(payload.get("username", "")).strip(),
        password=str(payload.get("password", "")).strip(),
        from_email=from_email,
        from_name=str(payload.get("from_name", get_runtime_config_value("APP_NAME", "WebApp"))).strip() or "WebApp",
        reply_to=str(payload.get("reply_to", from_email)).strip() or from_email,
        use_tls=_to_bool(payload.get("use_tls", True), True),
        use_ssl=_to_bool(payload.get("use_ssl", False), False),
        timeout_sec=_to_int(payload.get("timeout_sec", 15), 15, 3, 120),
    )


def send_email(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    rt = runtime_email_settings()
    if not rt.enabled:
        raise EmailServiceError("invio email disabilitato da config")
    if not to_email or "@" not in to_email:
        raise EmailServiceError("destinatario email non valido")
    msg = EmailMessage()
    msg["Subject"] = str(subject or "").strip()[:240]
    msg["From"] = f"{rt.from_name} <{rt.from_email}>"
    msg["To"] = str(to_email).strip().lower()
    msg["Reply-To"] = rt.reply_to
    msg.set_content(str(text_body or ""))
    if html_body:
        msg.add_alternative(str(html_body), subtype="html")

    if rt.mode == "disabled":
        raise EmailServiceError("email delivery mode is disabled")

    if rt.mode == "sendmail":
        candidates = []
        if rt.sendmail_path:
            candidates.append(rt.sendmail_path)
        candidates.extend(["/usr/sbin/sendmail", "/usr/lib/sendmail"])
        bin_path = ""
        for item in candidates:
            p = Path(item).expanduser()
            if p.exists() and p.is_file():
                bin_path = str(p)
                break
        if not bin_path:
            found = shutil.which("sendmail")
            if found:
                bin_path = found
        if not bin_path:
            raise EmailServiceError("sendmail non trovato nel sistema")
        try:
            proc = subprocess.run(
                [bin_path, "-t", "-oi"],
                input=msg.as_bytes(),
                check=False,
                capture_output=True,
                timeout=20,
            )
        except Exception as ex:
            raise EmailServiceError(f"errore invio sendmail: {ex}") from ex
        if int(proc.returncode) != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise EmailServiceError(f"sendmail exit={proc.returncode} {err[:180]}")
        return

    provider: ProviderEmailConfig
    if rt.smtp_host:
        provider = ProviderEmailConfig(
            smtp_host=rt.smtp_host,
            smtp_port=int(rt.smtp_port),
            username=rt.smtp_username,
            password=rt.smtp_password,
            from_email=rt.from_email,
            from_name=rt.from_name,
            reply_to=rt.reply_to,
            use_tls=bool(rt.smtp_use_tls),
            use_ssl=bool(rt.smtp_use_ssl),
            timeout_sec=int(rt.smtp_timeout_sec),
        )
    elif rt.config_path:
        provider = _load_provider_config(rt.config_path)
    else:
        raise EmailServiceError("SMTP mode requires SMTP_HOST/runtime SMTP settings or EMAIL_CONFIG_PATH")

    msg.replace_header("From", f"{provider.from_name} <{provider.from_email}>")
    msg.replace_header("Reply-To", provider.reply_to)
    if provider.use_ssl:
        with smtplib.SMTP_SSL(provider.smtp_host, provider.smtp_port, timeout=provider.timeout_sec) as smtp:
            if provider.username:
                smtp.login(provider.username, provider.password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(provider.smtp_host, provider.smtp_port, timeout=provider.timeout_sec) as smtp:
        smtp.ehlo()
        if provider.use_tls:
            smtp.starttls()
            smtp.ehlo()
        if provider.username:
            smtp.login(provider.username, provider.password)
        smtp.send_message(msg)
