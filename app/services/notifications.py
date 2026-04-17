"""Notification services: Slack board-room alerts and SES email delivery."""

import asyncio
import io
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
import httpx
import structlog
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Slack notifications
# ---------------------------------------------------------------------------


async def notify_session_complete(
    user_email: str,
    business_summary: str,
    session_id: str,
) -> None:
    """POST a session-completion alert to Slack #board-room.

    Args:
        user_email: The user's email address from signup.
        business_summary: One-line business summary extracted from the session.
        session_id: Unique session identifier for reference.
    """
    if not settings.SLACK_WEBHOOK_URL:
        logger.warning("slack_webhook_not_configured", session_id=session_id)
        return

    payload = {
        "channel": settings.SLACK_CHANNEL,
        "text": f"*New Tedi session completed* 🎙️",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*New session completed*\n"
                        f"• *User:* {user_email}\n"
                        f"• *Summary:* {business_summary}\n"
                        f"• *Session ID:* `{session_id}`"
                    ),
                },
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.SLACK_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
        logger.info("slack_notification_sent", session_id=session_id, user_email=user_email)
    except Exception as exc:
        # Non-fatal — log and continue
        logger.error("slack_notification_failed", session_id=session_id, error=str(exc))


# ---------------------------------------------------------------------------
# Email delivery — 2 output files
# ---------------------------------------------------------------------------


async def send_session_output_email(
    user_email: str,
    project_name: str,
    tdd_docx_bytes: bytes,
    claude_md_content: str,
) -> None:
    """Send 2 output files to the user within 5 minutes of session end.

    Files:
        - AI Agent Assessment as DOCX
        - CLAUDE.md as .md attachment

    Args:
        user_email: Recipient email address.
        project_name: Project/company name for subject line and filename.
        tdd_docx_bytes: AI Agent Assessment document as DOCX bytes.
        claude_md_content: CLAUDE.md file content.
    """
    safe_name = project_name.replace(" ", "_")

    msg = MIMEMultipart()
    msg["Subject"] = f"Your AI Agent Assessment — {project_name}"
    msg["From"] = settings.SES_FROM_EMAIL
    msg["To"] = user_email

    body = (
        f"Hi,\n\n"
        f"Here are the outputs from your Tedi AI discovery session for {project_name}.\n\n"
        f"Attached:\n"
        f"  1. AI Agent Assessment — {safe_name}-Assessment.docx\n"
        f"  2. CLAUDE.md — ready to drop into Claude Code to start building\n\n"
        f"Want to talk through the findings and map out next steps?\n"
        f"Book a call with the BonEcho team: {settings.BOOKING_URL}\n\n"
        f"— Tedi, BonEcho"
    )
    msg.attach(MIMEText(body, "plain"))

    # Attach Assessment DOCX
    tdd_attachment = MIMEApplication(tdd_docx_bytes)
    tdd_attachment.add_header("Content-Disposition", "attachment", filename=f"{safe_name}-Assessment.docx")
    msg.attach(tdd_attachment)

    # Attach CLAUDE.md
    _attach_text(msg, claude_md_content.encode(), "CLAUDE.md")

    # Send to user + internal team
    recipients = [user_email]
    for r in settings.OUTPUT_RECIPIENTS.split(","):
        r = r.strip()
        if r and r not in recipients:
            recipients.append(r)

    await _send_raw_email(settings.SES_FROM_EMAIL, recipients, msg)
    logger.info("session_output_email_sent", recipients=recipients, project_name=project_name)


def _attach_text(msg: MIMEMultipart, content: bytes, filename: str) -> None:
    part = MIMEApplication(content)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)


async def _send_raw_email(
    source: str,
    destinations: list[str],
    msg: MIMEMultipart,
) -> None:
    """Send a raw email via AWS SES."""

    def _send() -> None:
        ses = boto3.client("ses", region_name=settings.AWS_REGION)
        ses.send_raw_email(
            Source=source,
            Destinations=destinations,
            RawMessage={"Data": msg.as_string()},
        )

    try:
        await asyncio.to_thread(_send)
    except (BotoCoreError, ClientError) as exc:
        logger.error("ses_send_failed", destinations=destinations, error=str(exc))
        raise
