import uuid
from datetime import date

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.session import Session
from app.models.user import User

logger = structlog.get_logger(__name__)


class SignupService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_daily_session_count(self) -> int:
        result = await self._db.execute(
            text(
                "SELECT COUNT(*) FROM sessions "
                "WHERE DATE(created_at) = CURRENT_DATE "
                "AND status != 'ERROR'"
            )
        )
        return result.scalar_one()

    async def get_waitlist_position(self) -> int:
        """Return approximate queue position (count of waitlisted signups today + 1)."""
        # We don't persist waitlist entries; position is estimated from overflow count.
        count = await self.get_daily_session_count()
        return max(1, count - settings.DAILY_SESSION_CAP + 1)

    async def upsert_user(self, email: str) -> User:
        result = await self._db.execute(
            select(User).where(User.email == email)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(email=email)
            self._db.add(user)
            await self._db.flush()
            logger.info("user_created", user_id=str(user.id), email=email)
        else:
            logger.info("user_found", user_id=str(user.id), email=email)
        return user

    async def create_session(self, user_id: uuid.UUID) -> Session:
        session = Session(
            user_id=user_id,
            token=uuid.uuid4(),
            status="CREATED",
        )
        self._db.add(session)
        await self._db.flush()
        logger.info("session_created", session_id=str(session.id), user_id=str(user_id))
        return session

    async def signup(
        self, email: str
    ) -> tuple[str, Session | None, int | None]:
        """
        Returns (outcome, session_or_none, waitlist_position_or_none).
        outcome: "created" | "waitlisted"
        """
        daily_count = await self.get_daily_session_count()

        if daily_count >= settings.DAILY_SESSION_CAP:
            position = await self.get_waitlist_position()
            logger.info(
                "signup_waitlisted",
                email=email,
                daily_count=daily_count,
                cap=settings.DAILY_SESSION_CAP,
                position=position,
            )
            return "waitlisted", None, position

        user = await self.upsert_user(email)
        session = await self.create_session(user.id)
        await self._db.commit()
        return "created", session, None
