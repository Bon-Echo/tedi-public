"""Off-hot-path persistence of conversation turns into Postgres.

Each finalized turn (user transcript or agent spoken response) is fire-and-
forget written to `session_turns` so the admin dashboard can render the full
conversation later. The live WebSocket pipeline is never blocked on the DB
write — failures are logged and dropped.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_factory
from app.models.session_turn import SessionTurn

logger = structlog.get_logger(__name__)


def schedule_persist_turn(
    session_id: str | uuid.UUID,
    seq: int,
    speaker: str,
    text: str,
) -> asyncio.Task | None:
    """Schedule an async DB insert of a single conversation turn.

    Returns the task (or None if the input is empty / the loop is closed).
    """
    if not text or not text.strip():
        return None
    if speaker not in ("user", "agent"):
        logger.warning("turn_persistence_unknown_speaker", speaker=speaker)
        return None

    try:
        return asyncio.create_task(
            _persist_turn(str(session_id), seq, speaker, text)
        )
    except RuntimeError:
        # No running event loop (e.g. shutdown). Drop the write — the live
        # voice flow is the priority and we cannot block on Postgres here.
        logger.warning("turn_persistence_no_loop", session_id=str(session_id))
        return None


async def _persist_turn(
    session_id: str, seq: int, speaker: str, text: str
) -> None:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        # Some test/dev session ids are not valid UUIDs — skip persistence.
        logger.debug("turn_persistence_non_uuid_session", session_id=session_id)
        return

    try:
        async with async_session_factory() as db:
            stmt = (
                pg_insert(SessionTurn)
                .values(
                    session_id=sid,
                    seq=seq,
                    speaker=speaker,
                    text=text,
                )
                .on_conflict_do_nothing(
                    constraint="uq_session_turns_session_seq",
                )
            )
            await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.exception(
            "turn_persistence_failed",
            session_id=session_id,
            seq=seq,
            speaker=speaker,
        )
