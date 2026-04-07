"""Notification services: Slack board-room alerts and SES email delivery."""

import asyncio
import os
import re
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
import httpx
import structlog
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.models.artifacts import SessionArtifacts

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
# Email delivery — HTML + plain-text, dual-send
# ---------------------------------------------------------------------------


async def send_session_output_email(
    user_email: str,
    project_name: str,
    artifacts: SessionArtifacts,
) -> None:
    """Send discovery doc and CLAUDE.md to user, and a copy to BonEcho internal.

    Builds an HTML email with plain-text fallback. Attaches only the discovery
    doc and CLAUDE.md. Includes a CTA booking link if BONECHO_BOOKING_URL is
    configured. Sends a separate internal copy to BONECHO_INTERNAL_EMAIL with
    an [Internal] subject prefix if that setting is configured.

    Args:
        user_email: Recipient email address.
        project_name: Project name for subject line and email copy.
        artifacts: SessionArtifacts with discovery doc and CLAUDE.md content.
    """
    subject = f"Your Tedi session output — {project_name}"

    html_body = _render_email_html(
        project_name=project_name,
        discovery_doc_filename=artifacts.discovery_doc_filename,
        claude_md_filename=artifacts.claude_md_filename,
        booking_url=settings.BONECHO_BOOKING_URL,
    )
    plain_body = _render_email_plain(
        project_name=project_name,
        discovery_doc_filename=artifacts.discovery_doc_filename,
        claude_md_filename=artifacts.claude_md_filename,
        booking_url=settings.BONECHO_BOOKING_URL,
    )

    # --- User email ---
    user_msg = _build_message(
        subject=subject,
        from_addr=settings.SES_FROM_EMAIL,
        to_addr=user_email,
        html_body=html_body,
        plain_body=plain_body,
        artifacts=artifacts,
    )
    await _send_raw_email(settings.SES_FROM_EMAIL, [user_email], user_msg)
    logger.info("session_output_email_sent", recipient=user_email, project_name=project_name)

    # --- Internal email (separate send, [Internal] prefix) ---
    if settings.BONECHO_INTERNAL_EMAIL:
        internal_msg = _build_message(
            subject=f"[Internal] {subject}",
            from_addr=settings.SES_FROM_EMAIL,
            to_addr=settings.BONECHO_INTERNAL_EMAIL,
            html_body=html_body,
            plain_body=plain_body,
            artifacts=artifacts,
        )
        await _send_raw_email(
            settings.SES_FROM_EMAIL,
            [settings.BONECHO_INTERNAL_EMAIL],
            internal_msg,
        )
        logger.info(
            "session_output_internal_email_sent",
            recipient=settings.BONECHO_INTERNAL_EMAIL,
            project_name=project_name,
        )


def _build_message(
    subject: str,
    from_addr: str,
    to_addr: str,
    html_body: str,
    plain_body: str,
    artifacts: SessionArtifacts,
) -> MIMEMultipart:
    """Build a MIME multipart/mixed message with alternative text/HTML and attachments."""
    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject
    outer["From"] = from_addr
    outer["To"] = to_addr

    # Text alternatives
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    outer.attach(alt)

    # Discovery doc attachment
    doc_part = MIMEApplication(artifacts.discovery_doc)
    doc_part.add_header(
        "Content-Disposition", "attachment", filename=artifacts.discovery_doc_filename
    )
    doc_part.add_header("Content-Type", artifacts.discovery_doc_mime)
    outer.attach(doc_part)

    # CLAUDE.md attachment
    claude_part = MIMEApplication(artifacts.claude_md.encode("utf-8"))
    claude_part.add_header(
        "Content-Disposition", "attachment", filename=artifacts.claude_md_filename
    )
    outer.attach(claude_part)

    return outer


def _render_email_html(
    project_name: str,
    discovery_doc_filename: str,
    claude_md_filename: str,
    booking_url: str,
) -> str:
    """Render the HTML email template with simple string substitution."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "session_email.html"
    )
    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    # Replace template variables
    html = html.replace("{{ project_name }}", _escape_html(project_name))
    html = html.replace("{{ discovery_doc_filename }}", _escape_html(discovery_doc_filename))
    html = html.replace("{{ claude_md_filename }}", _escape_html(claude_md_filename))

    if booking_url:
        html = html.replace("{{ booking_url }}", _escape_html(booking_url))
        # Remove Jinja-style block tags
        html = re.sub(r"\{%\s*if booking_url\s*%\}", "", html)
        html = re.sub(r"\{%\s*endif\s*%\}", "", html)
    else:
        # Remove entire CTA block
        html = re.sub(
            r"\{%\s*if booking_url\s*%\}.*?\{%\s*endif\s*%\}",
            "",
            html,
            flags=re.DOTALL,
        )

    return html


def _render_email_plain(
    project_name: str,
    discovery_doc_filename: str,
    claude_md_filename: str,
    booking_url: str,
) -> str:
    """Build plain-text fallback body."""
    lines = [
        "Hi,",
        "",
        f"Here are the outputs from your Tedi discovery session for {project_name}.",
        "",
        "Attached files:",
        f"  - {discovery_doc_filename} (Technical Design Document)",
        f"  - {claude_md_filename} (ready to drop into Claude Code)",
        "",
    ]
    if booking_url:
        lines += [
            "Book your next session:",
            f"  {booking_url}",
            "",
        ]
    lines += [
        "If you have any questions, reply to this email.",
        "",
        "— Tedi, BonEcho",
    ]
    return "\n".join(lines)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
