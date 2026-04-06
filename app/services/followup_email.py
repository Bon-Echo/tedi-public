"""Follow-up email service — sends a personal 24hr post-session email.

This module is invoked by the systemd cron worker (app/cron/followup_worker.py).
It queries the database for sessions that completed ~24 hours ago and have not
yet received a follow-up email, then sends a short personal email from
sifat@bonecho.ai.
"""

import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = structlog.get_logger(__name__)


async def send_pending_followups(db: AsyncSession) -> int:
    """Query for sessions due a follow-up email and send them.

    Selects sessions where:
    - status = 'completed'
    - ended_at is between 23.5 and 24.5 hours ago
    - followup_sent_at IS NULL

    Returns:
        Number of follow-up emails sent.
    """
    query = text(
        """
        SELECT s.id, s.user_id, u.email, s.business_summary
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.status = 'completed'
          AND s.ended_at BETWEEN NOW() - INTERVAL '24.5 hours'
                               AND NOW() - INTERVAL '23.5 hours'
          AND s.followup_sent_at IS NULL
        """
    )

    result = await db.execute(query)
    rows = result.fetchall()

    sent = 0
    for row in rows:
        session_id, user_id, email, summary = row
        try:
            await _send_followup(email, summary)
            await db.execute(
                text(
                    "UPDATE sessions SET followup_sent_at = NOW() WHERE id = :id"
                ),
                {"id": session_id},
            )
            await db.commit()
            sent += 1
            logger.info("followup_email_sent", session_id=str(session_id), email=email)
        except Exception as exc:
            logger.error(
                "followup_email_failed",
                session_id=str(session_id),
                email=email,
                error=str(exc),
            )

    return sent


async def _send_followup(user_email: str, business_summary: str | None) -> None:
    """Send a short personal follow-up email from sifat@bonecho.ai."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Tedi session"
    msg["From"] = settings.FOLLOWUP_FROM_EMAIL
    msg["To"] = user_email

    body = (
        "Hey, just checking in — did you get a chance to look through the files from your session? "
        "Happy to answer any questions.\n\n"
        "— Sifat"
    )
    msg.attach(MIMEText(body, "plain"))

    def _send() -> None:
        ses = boto3.client("ses", region_name=settings.AWS_REGION)
        ses.send_raw_email(
            Source=settings.FOLLOWUP_FROM_EMAIL,
            Destinations=[user_email],
            RawMessage={"Data": msg.as_string()},
        )

    await asyncio.to_thread(_send)
