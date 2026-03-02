from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import render_template, url_for

from ..models import BroadcastMessage, BroadcastMessageRead, User, UserMessage, now_utc
from ..utils import validate_action_url
from .email_service import send_email


DEFAULT_MESSAGE_EMAIL_TEMPLATE = "default"


@dataclass(frozen=True)
class MessageEmailTemplate:
    key: str
    label: str
    description: str


MESSAGE_EMAIL_TEMPLATES: tuple[MessageEmailTemplate, ...] = (
    MessageEmailTemplate("default", "Default", "Neutral transactional message card."),
    MessageEmailTemplate("alert", "Alert", "High attention message with stronger warning styling."),
    MessageEmailTemplate("success", "Success", "Positive notification with success styling."),
)


def get_message_email_templates() -> list[dict[str, str]]:
    return [{"key": item.key, "label": item.label, "description": item.description} for item in MESSAGE_EMAIL_TEMPLATES]


def render_message_email(
    *,
    template_key: str | None,
    recipient_name: str,
    title: str,
    body: str,
    body_format: str,
    level: str,
    preheader: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
) -> tuple[str, str]:
    key = str(template_key or DEFAULT_MESSAGE_EMAIL_TEMPLATE).strip().lower() or DEFAULT_MESSAGE_EMAIL_TEMPLATE
    template_name = f"email/messages/{key}/message.html"
    safe_body_format = "html" if str(body_format or "").strip().lower() == "html" else "text"
    inbox_url = url_for("main.messages", _external=True)
    text_body = (
        f"{title}\n\n"
        f"{body if safe_body_format == 'text' else 'You have received a new message in WebApp.'}\n\n"
        f"Open inbox: {inbox_url}"
    )
    html_body = render_template(
        template_name,
        title=title,
        body=body,
        body_format=safe_body_format,
        level=level,
        recipient_name=recipient_name,
        preheader=(preheader or title or "WebApp notification")[:180],
        action_label=(action_label or "").strip()[:80],
        action_url=validate_action_url(action_url) or inbox_url,
        inbox_url=inbox_url,
    )
    return text_body, html_body


def send_message_email(
    *,
    recipient: User,
    title: str,
    body: str,
    body_format: str,
    level: str,
    template_key: str | None = None,
    subject: str | None = None,
    preheader: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
) -> None:
    text_body, html_body = render_message_email(
        template_key=template_key,
        recipient_name=str(recipient.name or recipient.email or "User"),
        title=title,
        body=body,
        body_format=body_format,
        level=level,
        preheader=preheader,
        action_label=action_label,
        action_url=action_url,
    )
    send_email(
        to_email=recipient.email,
        subject=(subject or title or "WebApp message")[:180],
        text_body=text_body,
        html_body=html_body,
    )


def unread_message_counts_for_user(user_id: int | None) -> dict[str, int]:
    uid = int(user_id or 0)
    if uid <= 0:
        return {"unread_total": 0, "unread_user": 0, "unread_broadcast": 0}

    unread_user = int(
        UserMessage.query.filter_by(user_id=uid, is_read=False, is_active=True)
        .filter((UserMessage.expires_at.is_(None)) | (UserMessage.expires_at >= now_utc()))
        .count()
    )
    read_query = BroadcastMessageRead.query.with_entities(BroadcastMessageRead.message_id).filter_by(user_id=uid)
    unread_broadcast = int(
        BroadcastMessage.query.filter_by(is_active=True)
        .filter((BroadcastMessage.expires_at.is_(None)) | (BroadcastMessage.expires_at >= now_utc()))
        .filter(~BroadcastMessage.id.in_(read_query))
        .count()
    )
    return {
        "unread_total": unread_user + unread_broadcast,
        "unread_user": unread_user,
        "unread_broadcast": unread_broadcast,
    }
