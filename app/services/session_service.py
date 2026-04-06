"""Session service for database session state transitions."""

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session

logger = structlog.get_logger(__name__)


class SessionService:
    """Handles database-backed session lifecycle transitions."""

    async def get_session_by_token(
        self,
        token: uuid.UUID,
        db: AsyncSession,
    ) -> Session | None:
        """Retrieve a session by its auth token."""
        result = await db.execute(select(Session).where(Session.token == token))
        return result.scalar_one_or_none()

    async def transition_to_active(
        self,
        session: Session,
        db: AsyncSession,
    ) -> None:
        """Transition a CREATED session to ACTIVE on first WebSocket connect."""
        session.status = "ACTIVE"
        session.started_at = datetime.now(timezone.utc)
        db.add(session)
        await db.commit()
        logger.info("session_activated", session_id=str(session.id))

    async def transition_to_ended(
        self,
        session: Session,
        db: AsyncSession,
    ) -> None:
        """Transition an ACTIVE session to ENDED on WebSocket disconnect."""
        session.status = "ENDED"
        session.ended_at = datetime.now(timezone.utc)
        db.add(session)
        await db.commit()
        logger.info("session_ended", session_id=str(session.id))

    async def transition_to_timed_out(
        self,
        session: Session,
        db: AsyncSession,
    ) -> None:
        """Transition an ACTIVE session to TIMED_OUT when session duration expires."""
        session.status = "TIMED_OUT"
        session.ended_at = datetime.now(timezone.utc)
        db.add(session)
        await db.commit()
        logger.info("session_timed_out", session_id=str(session.id))
