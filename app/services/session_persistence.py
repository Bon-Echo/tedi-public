"""Persist post-session artifact + summary state onto the sessions row.

The post-session pipeline runs as a fire-and-forget asyncio task after the
WebSocket disconnects, so this helper opens its own AsyncSession and commits
the update independently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import update

from app.database import async_session_factory
from app.models.session import Session as DBSession

logger = structlog.get_logger(__name__)


@dataclass
class SessionCompletionRecord:
    session_id: str
    tdd_s3_key: str | None
    claude_md_s3_key: str | None
    summary: str | None
    business_summary: str | None
    email_sent: bool
    final_status: str


async def persist_session_completion(record: SessionCompletionRecord) -> None:
    """Update the sessions row with artifact + summary state and final status.

    `followup_sent_at` is intentionally NOT stamped here: that column is owned
    by the 24hr scheduled follow-up worker (see `app/services/followup_email.py`),
    which selects rows where `followup_sent_at IS NULL`. The initial artifact
    delivery and the delayed follow-up are distinct states.
    """
    try:
        sid = uuid.UUID(record.session_id)
    except ValueError:
        logger.debug(
            "session_completion_persist_non_uuid", session_id=record.session_id
        )
        return

    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "status": record.final_status,
        "ended_at": now,
        "updated_at": now,
    }
    if record.tdd_s3_key is not None:
        values["tdd_s3_key"] = record.tdd_s3_key
    if record.claude_md_s3_key is not None:
        values["claude_md_s3_key"] = record.claude_md_s3_key
    if record.summary is not None:
        values["summary"] = record.summary
    if record.business_summary is not None:
        values["business_summary"] = record.business_summary

    async with async_session_factory() as db:
        await db.execute(
            update(DBSession).where(DBSession.id == sid).values(**values)
        )
        await db.commit()
    logger.info(
        "session_completion_persisted",
        session_id=record.session_id,
        status=record.final_status,
        email_sent=record.email_sent,
    )
