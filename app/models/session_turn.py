import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SessionTurn(Base):
    """One finalized turn of a Tedi conversation, persisted for admin review."""

    __tablename__ = "session_turns"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_session_turns_session_seq"),
        Index("idx_session_turns_session_seq", "session_id", "seq"),
    )

    def __repr__(self) -> str:
        return f"<SessionTurn session={self.session_id} seq={self.seq} speaker={self.speaker}>"
