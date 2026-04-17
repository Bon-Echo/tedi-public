import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

SESSION_STATUSES = (
    "CREATED",
    "ACTIVE",
    "ENDED",
    "TIMED_OUT",
    "POST_PROCESSING",
    "COMPLETED",
    "ERROR",
)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    token: Mapped[uuid.UUID] = mapped_column(
        unique=True,
        default=uuid.uuid4,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="CREATED",
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    transcript_s3_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Generated artifacts (S3 keys within S3_BUCKET_NAME).
    tdd_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_md_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Summaries surfaced to the admin dashboard.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Follow-up tracking.
    followup_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_manual_followup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Session id={self.id} status={self.status}>"
