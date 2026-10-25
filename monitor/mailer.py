"""Send the report by email over SMTP (Gmail by default, any SMTP works).

Settings come from environment variables (GitHub Secrets):
  SMTP_USER      – the sending mailbox, e.g. myreports@gmail.com
  SMTP_PASSWORD  – for Gmail: an App Password (not the normal password)
  EMAIL_TO       – recipients, comma separated
  SMTP_HOST      – optional, default smtp.gmail.com
  SMTP_PORT      – optional, default 465 (SSL)
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)


def configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))


def send(subject: str, html_body: str, attachments: list[Path] | None = None, text_body: str | None = None) -> None:
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"].replace(" ", "")   # Gmail shows app passwords with spaces
    to = [a.strip() for a in os.environ["EMAIL_TO"].replace(";", ",").split(",") if a.strip()]
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"מעקב מחירים <{user}>"
    msg["To"] = ", ".join(to)
    msg.set_content(text_body or "הדוח מצורף כקובץ אקסל. לצפייה בגרסה המעוצבת יש לפתוח את המייל בתצוגת HTML.")
    msg.add_alternative(html_body, subtype="html")
    for p in attachments or []:
        msg.add_attachment(p.read_bytes(), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=p.name)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
    log.info("Email sent to %s", ", ".join(to))
