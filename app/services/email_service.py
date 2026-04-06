import asyncio
from typing import TYPE_CHECKING

import boto3
import structlog

from app.config import settings

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

CONFIRMATION_SUBJECT = "Your Tedi session is ready"

CONFIRMATION_BODY_TEXT = """\
Hi there,

Your Tedi session has been created. Join your session here:

{room_url}

See you soon,
The Tedi team
"""

CONFIRMATION_BODY_HTML = """\
<html>
<body>
<p>Hi there,</p>
<p>Your Tedi session has been created. Join your session here:</p>
<p><a href="{room_url}">{room_url}</a></p>
<p>See you soon,<br>The Tedi team</p>
</body>
</html>
"""


class EmailService:
    def __init__(self) -> None:
        self._client = boto3.client("ses", region_name=settings.SES_REGION)

    def _send_confirmation_sync(self, to_email: str, room_url: str) -> None:
        self._client.send_email(
            Source=settings.SES_FROM_EMAIL,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": CONFIRMATION_SUBJECT, "Charset": "UTF-8"},
                "Body": {
                    "Text": {
                        "Data": CONFIRMATION_BODY_TEXT.format(room_url=room_url),
                        "Charset": "UTF-8",
                    },
                    "Html": {
                        "Data": CONFIRMATION_BODY_HTML.format(room_url=room_url),
                        "Charset": "UTF-8",
                    },
                },
            },
        )

    async def send_confirmation(self, to_email: str, room_url: str) -> None:
        """Fire-and-forget: send confirmation email async. Failures are logged, not raised."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, self._send_confirmation_sync, to_email, room_url
            )
            logger.info("confirmation_email_sent", to=to_email)
        except Exception:
            logger.exception("confirmation_email_failed", to=to_email)


def send_confirmation_fire_and_forget(
    email_service: "EmailService", to_email: str, room_url: str
) -> None:
    """Schedule email sending as a background task without awaiting it."""
    asyncio.ensure_future(email_service.send_confirmation(to_email, room_url))
