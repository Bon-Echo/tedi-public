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
# Email delivery — 4 output files
# ---------------------------------------------------------------------------


async def send_session_output_email(
    user_email: str,
    project_name: str,
    tdd_docx_bytes: bytes,
    claude_md_content: str,
    skills_content: str,
    context_content: str,
) -> None:
    """Send 4 output files to the user within 5 minutes of session end.

    Files:
        - TDD as DOCX
        - CLAUDE.md as .md attachment
        - Skills file as .md attachment
        - Context file as .md attachment

    Args:
        user_email: Recipient email address.
        project_name: Project name for subject line and filename.
        tdd_docx_bytes: TDD document as DOCX bytes.
        claude_md_content: CLAUDE.md file content.
        skills_content: Skills file content.
        context_content: Context file content.
    """
    safe_name = project_name.replace(" ", "_")

    msg = MIMEMultipart()
    msg["Subject"] = f"Your Tedi session output — {project_name}"
    msg["From"] = settings.SES_FROM_EMAIL
    msg["To"] = user_email

    body = (
        f"Hi,\n\n"
        f"Here are the outputs from your Tedi discovery session for {project_name}.\n\n"
        f"Attached:\n"
        f"  1. Technical Design Document (TDD) — {safe_name}-TDD.docx\n"
        f"  2. CLAUDE.md — ready to drop into Claude Code\n"
        f"  3. Skills file — agent skills extracted from your session\n"
        f"  4. Context file — business background and key details\n\n"
        f"If you have questions, just reply to this email.\n\n"
        f"— Tedi, BonEcho"
    )
    msg.attach(MIMEText(body, "plain"))

    # Attach TDD DOCX
    tdd_attachment = MIMEApplication(tdd_docx_bytes)
    tdd_attachment.add_header("Content-Disposition", "attachment", filename=f"{safe_name}-TDD.docx")
    msg.attach(tdd_attachment)

    # Attach CLAUDE.md
    _attach_text(msg, claude_md_content.encode(), "CLAUDE.md")

    # Attach skills file
    _attach_text(msg, skills_content.encode(), f"{safe_name}-skills.md")

    # Attach context file
    _attach_text(msg, context_content.encode(), f"{safe_name}-context.md")

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
