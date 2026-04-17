"""Tests for the manual follow-up admin endpoint.

`asyncio.to_thread(_send)` inside the SES helper is patched out so we can
verify the full flow (audit + DB update + response) without hitting AWS.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import get_session
from app.middleware.admin_auth import ADMIN_COOKIE_NAME, issue_session_cookie
from app.routers import admin_api as admin_router
from app.services import ondemand_followup


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def db():
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
    Table(
        "session_turns",
        md,
        Column("id", String, primary_key=True),
        Column("session_id", String, ForeignKey("sessions.id"), nullable=False),
        Column("seq", Integer, nullable=False),
        Column("speaker", String, nullable=False),
        Column("text", Text, nullable=False),
        Column(
            "created_at",
            DateTime,
            server_default=text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    Table(
        "admin_audit",
        md,
        Column("id", String, primary_key=True),
        Column("actor_email", String, nullable=False),
        Column("action", String, nullable=False),
        Column("target_session_id", String, nullable=True),
        Column("target_user_id", String, nullable=True),
        Column("note", Text, nullable=True),
        Column("metadata_json", Text, nullable=True),
        Column(
            "created_at",
            DateTime,
            server_default=text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(md.create_all)

    asyncio.get_event_loop().run_until_complete(_create())

    user_uuid = uuid.uuid4()
    session_uuid = uuid.uuid4()
    user_id = user_uuid.hex
    session_id = session_uuid.hex
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
                    "(:id, :uid, :tok, 'COMPLETED', :now, :now)"
                ),
                {
                    "id": session_id,
                    "uid": user_id,
                    "tok": str(uuid.uuid4()),
                    "now": now,
                },
            )
            await s.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    return {
        "engine": engine,
        "factory": factory,
        "user_id": str(user_uuid),
        "session_id": str(session_uuid),
        "session_id_hex": session_id,
    }


@pytest.fixture
def app(db):
    factory = db["factory"]

    async def override_get_session():
        async with factory() as s:
            yield s

    fa = FastAPI()
    fa.include_router(admin_router.router)
    fa.dependency_overrides[get_session] = override_get_session
    return fa


@pytest.fixture
def admin_cookie():
    response = Response()
    token = issue_session_cookie(response, "ops@bonecho.ai")
    return {ADMIN_COOKIE_NAME: token}


@pytest.fixture(autouse=True)
def stub_ses(monkeypatch):
    """Replace asyncio.to_thread used by ondemand_followup so SES never runs."""
    sent = {"called": False, "destinations": None}

    async def fake_to_thread(fn, *args, **kwargs):
        sent["called"] = True
        return None

    monkeypatch.setattr(ondemand_followup.asyncio, "to_thread", fake_to_thread)
    return sent


def test_manual_followup_sends_records_audit_and_stamps_session(
    app, db, admin_cookie, stub_ses
):
    client = TestClient(app, cookies=admin_cookie)
    r = client.post(
        f"/api/admin/sessions/{db['session_id']}/followup",
        json={"subject": "checking in", "body": "hey, any questions?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "auditId" in body and body["auditId"]
    assert stub_ses["called"] is True

    factory = db["factory"]

    async def _check():
        async with factory() as s:
            ts = (
                await s.execute(
                    text(
                        "SELECT last_manual_followup_at FROM sessions WHERE id = :sid"
                    ),
                    {"sid": db["session_id_hex"]},
                )
            ).scalar_one()
            audit_count = (
                await s.execute(
                    text(
                        "SELECT COUNT(*) FROM admin_audit "
                        "WHERE action = 'admin.session.followup' "
                        "AND target_session_id = :sid"
                    ),
                    {"sid": db["session_id_hex"]},
                )
            ).scalar_one()
            return ts, audit_count

    ts, audit_count = asyncio.get_event_loop().run_until_complete(_check())
    assert ts is not None
    assert audit_count == 1


def test_manual_followup_validates_empty_body(app, db, admin_cookie):
    client = TestClient(app, cookies=admin_cookie)
    r = client.post(
        f"/api/admin/sessions/{db['session_id']}/followup",
        json={"body": ""},
    )
    # Pydantic min_length=1 surfaces as 422.
    assert r.status_code == 422
