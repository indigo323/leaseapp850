"""
Email delivery.

Configured via env. If SMTP_HOST is empty, emails are logged instead of
sent — handy during local dev. In production, Claude Code will wire this
up to whatever SMTP relay (or submission service) is already configured
on this server.

Env:
  SMTP_HOST       mx.example.com (empty → log-only mode)
  SMTP_PORT       587
  SMTP_USER       smtp user
  SMTP_PASSWORD   smtp password
  SMTP_STARTTLS   "true" | "false"  (default true)
  SMTP_FROM       "Lease App <lease@heaveto.net>"
"""

from __future__ import annotations

import logging
import os
from email.message import EmailMessage

import aiosmtplib


log = logging.getLogger("lease-app.email")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "true").lower() == "true"
SMTP_FROM = os.environ.get("SMTP_FROM", "lease@localhost")


async def send(
    *,
    to: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
) -> bool:
    """
    Send a plain-text email. Returns True if delivered (or log-only mode),
    False on error. Doesn't raise — email failures should never block the
    main workflow.
    """
    if not SMTP_HOST:
        log.warning("SMTP not configured; would have sent email:\n"
                    "  To: %s\n  Subject: %s\n  Body:\n%s",
                    to, subject, body)
        return True

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER or None,
            password=SMTP_PASSWORD or None,
            start_tls=SMTP_STARTTLS,
            timeout=30,
        )
        log.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        log.error("Email delivery failed to %s: %s", to, e)
        return False
