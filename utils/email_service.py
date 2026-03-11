"""Reusable email sending helpers based on Django SMTP settings."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def send_system_email(
    subject: str,
    body: str,
    recipients: Iterable[str],
    *,
    html_body: str | None = None,
    from_email: str | None = None,
    fail_silently: bool = False,
) -> int:
    """Send an email to one or many recipients via configured SMTP backend."""
    to_list = [email.strip() for email in recipients if email and email.strip()]
    if not to_list:
        logger.warning("Email skipped: recipients list is empty")
        return 0

    sender = (from_email or settings.DEFAULT_FROM_EMAIL).strip()
    message = EmailMultiAlternatives(
        subject=(subject or "").strip(),
        body=body or "",
        from_email=sender,
        to=to_list,
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")

    sent_count = message.send(fail_silently=fail_silently)
    logger.info("Email sent subject=%s recipients=%s sent=%s", subject, len(to_list), sent_count)
    return sent_count

