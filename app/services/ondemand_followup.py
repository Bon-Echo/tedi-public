"""Admin-triggered manual follow-up email.

Reuses the existing SES path. Caller (admin router) is responsible for:
- authenticating the actor
- writing the audit row
- committing the DB transaction
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.session import Session as DBSession

logger = structlog.get_logger(__name__)


@dataclass
class ManualFollowupResult:
    sent: bool
    sent_at: datetime
    recipient: str
    subject: str


async def send_manual_followup(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    recipient_email: str,
    body: str,
    subject: str | None = None,
) -> ManualFollowupResult:
    """Send a manual follow-up email via SES and stamp `last_manual_followup_at`."""
    if not body or not body.strip():
        raise ValueError("manual followup body must not be empty")

    final_subject = (subject or "Following up on your Tedi session").strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = final_subject
    msg["From"] = settings.FOLLOWUP_FROM_EMAIL
    msg["To"] = recipient_email
    msg.attach(MIMEText(body, "plain"))

    def _send() -> None:
        ses = boto3.client("ses", region_name=settings.AWS_REGION)
        ses.send_raw_email(
            Source=settings.FOLLOWUP_FROM_EMAIL,
            Destinations=[recipient_email],
            RawMessage={"Data": msg.as_string()},
        )

    try:
        await asyncio.to_thread(_send)
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "manual_followup_ses_failed",
            session_id=str(session_id),
            recipient=recipient_email,
            error=str(exc),
        )
        raise

    sent_at = datetime.now(timezone.utc)
    await db.execute(
        update(DBSession)
        .where(DBSession.id == session_id)
        .values(last_manual_followup_at=sent_at)
    )

    logger.info(
        "manual_followup_sent",
        session_id=str(session_id),
        recipient=recipient_email,
    )
    return ManualFollowupResult(
        sent=True,
        sent_at=sent_at,
        recipient=recipient_email,
        subject=final_subject,
    )
