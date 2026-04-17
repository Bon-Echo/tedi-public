"""Tests for the post-session artifact/summary persistence helper.

These tests pin down the contract between the immediate post-session output
email (initial artifact delivery) and the scheduled 24-hour follow-up worker.

The scheduled worker in `app/services/followup_email.py` selects rows where
`followup_sent_at IS NULL`, so the immediate artifact delivery MUST NOT stamp
`followup_sent_at` — doing so would silently suppress every scheduled
follow-up.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services import session_persistence
from app.services.session_persistence import (
    SessionCompletionRecord,
    persist_session_completion,
)


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    md = MetaData()
    Table(
        "users",
        md,
        Column("id", String, primary_key=True),
        Column("email", String, nullable=False, unique=True),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    Table(
        "sessions",
        md,
        Column("id", String, primary_key=True),
        Column("user_id", String, ForeignKey("users.id"), nullable=False),
        Column("token", String, nullable=False),
        Column("status", String, nullable=False),
        Column("started_at", DateTime, nullable=True),
        Column("ended_at", DateTime, nullable=True),
        Column("transcript_s3_uri", Text, nullable=True),
        Column("tdd_s3_key", Text, nullable=True),
        Column("claude_md_s3_key", Text, nullable=True),
        Column("summary", Text, nullable=True),
        Column("business_summary", Text, nullable=True),
        Column("followup_sent_at", DateTime, nullable=True),
        Column("last_manual_followup_at", DateTime, nullable=True),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(md.create_all)

    asyncio.get_event_loop().run_until_complete(_create())

    user_uuid = uuid.uuid4()
    session_uuid = uuid.uuid4()
    user_id = user_uuid.hex
    session_id_hex = session_uuid.hex
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _seed():
        async with factory() as s:
            await s.execute(
                text(
                    "INSERT INTO users (id, email, created_at, updated_at) "
                    "VALUES (:id, :em, :now, :now)"
                ),
                {"id": user_id, "em": "founder@example.com", "now": now},
            )
            await s.execute(
                text(
                    "INSERT INTO sessions (id, user_id, token, status, "
                    "created_at, updated_at) VALUES "
                    "(:id, :uid, :tok, 'POST_PROCESSING', :now, :now)"
                ),
                {
                    "id": session_id_hex,
                    "uid": user_id,
                    "tok": str(uuid.uuid4()),
                    "now": now,
                },
            )
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    monkeypatch.setattr(session_persistence, "async_session_factory", factory)

    return {
        "factory": factory,
        "session_id": str(session_uuid),
        "session_id_hex": session_id_hex,
    }


def test_persist_completion_preserves_scheduled_followup_eligibility(db):
    """Initial artifact email must NOT stamp followup_sent_at.

    The 24-hour scheduled follow-up worker filters on `followup_sent_at IS NULL`;
    stamping this column during the initial artifact delivery would silently
    suppress every scheduled follow-up email.
    """
    record = SessionCompletionRecord(
        session_id=db["session_id"],
        tdd_s3_key="sessions/abc/tdd.docx",
        claude_md_s3_key="sessions/abc/CLAUDE.md",
        summary="exec summary",
        business_summary="biz summary",
        email_sent=True,
        final_status="COMPLETED",
    )

    asyncio.get_event_loop().run_until_complete(persist_session_completion(record))

    async def _read():
        async with db["factory"]() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT status, ended_at, followup_sent_at, "
                        "tdd_s3_key, claude_md_s3_key, summary, business_summary "
                        "FROM sessions WHERE id = :sid"
                    ),
                    {"sid": db["session_id_hex"]},
                )
            ).one()
            return row

    row = asyncio.get_event_loop().run_until_complete(_read())
    status, ended_at, followup_sent_at, tdd_key, claude_key, summary, biz = row

    # Artifact + summary state should be persisted.
    assert status == "COMPLETED"
    assert ended_at is not None
    assert tdd_key == "sessions/abc/tdd.docx"
    assert claude_key == "sessions/abc/CLAUDE.md"
    assert summary == "exec summary"
    assert biz == "biz summary"

    # Critical invariant: scheduled 24hr follow-up must remain eligible.
    assert followup_sent_at is None, (
        "persist_session_completion must not stamp followup_sent_at for the "
        "immediate artifact email — doing so suppresses the scheduled 24hr "
        "follow-up worker which filters on followup_sent_at IS NULL."
    )
