"""Optional SMTP mailer. Every caller must work without it (e.g. invites fall back to a link)."""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import app.config as app_config

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(app_config.SMTP_HOST and app_config.SMTP_FROM)


def _send_sync(to: str, subject: str, text: str, html: str | None) -> None:
    msg = EmailMessage()
    msg["From"] = app_config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(app_config.SMTP_HOST, app_config.SMTP_PORT, timeout=20) as smtp:
        if app_config.SMTP_STARTTLS:
            smtp.starttls()
        if app_config.SMTP_USERNAME:
            smtp.login(app_config.SMTP_USERNAME, app_config.SMTP_PASSWORD or "")
        smtp.send_message(msg)


async def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send an email; returns False (and logs) instead of raising when it fails."""
    if not is_configured():
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, text, html)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("mail to %s failed: %s", to, exc)
        return False
