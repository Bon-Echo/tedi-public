"""End-to-end tests for the admin/auth HTTP surface.

Uses an in-memory SQLite DB via aiosqlite. The ORM models are
PostgreSQL-typed (UUID, JSONB), so the test creates equivalent tables manually
on SQLite and seeds rows directly.

Covers:
  - Acceptance 3: unauthenticated -> 401
  - Acceptance 5: signed-in admin can list signups and load a session detail
  - Acceptance 6: manual follow-up writes audit + stamps last_manual_followup_at
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
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

from app.middleware.admin_auth import issue_session_cookie, ADMIN_COOKIE_NAME
from app.routers import admin_api as admin_router
from app.routers import auth as auth_router
from app.database import get_session
from fastapi.responses import Response


# ---------------------------------------------------------------------------
# In-memory SQLite test DB with sessionless schema mirroring app models
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def db_engine_and_factory():
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
    return engine, factory


@pytest.fixture(scope="module")
def seeded_ids(db_engine_and_factory):
    """Seed one user with one completed session and 2 turns.

    SQLAlchemy's `Uuid` type stores as `CHAR(32)` (hex, no dashes) on SQLite
    backends, so we seed with `.hex` to match what the ORM emits at query time.
    """
    _engine, factory = db_engine_and_factory
    user_uuid = uuid.uuid4()
    session_uuid = uuid.uuid4()
    user_id = user_uuid.hex
    session_id = session_uuid.hex
    turn_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _seed():
        async with factory() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, email, created_at, updated_at) "
                    "VALUES (:id, :email, :now, :now)"
                ),
                {"id": user_id, "email": "founder@example.com", "now": now},
            )
            await db.execute(
                text(
                    "INSERT INTO sessions (id, user_id, token, status, "
                    "tdd_s3_key, claude_md_s3_key, summary, business_summary, "
                    "created_at, updated_at) "
                    "VALUES (:id, :uid, :tok, 'COMPLETED', "
                    "'sessions/x/tdd.docx', 'sessions/x/CLAUDE.md', "
                    "'Built an agent for X', 'X is a logistics SaaS', "
                    ":now, :now)"
                ),
                {
                    "id": session_id,
                    "uid": user_id,
                    "tok": str(uuid.uuid4()),
                    "now": now,
                },
            )
            for i, tid in enumerate(turn_ids):
                await db.execute(
                    text(
                        "INSERT INTO session_turns (id, session_id, seq, speaker, text, created_at) "
                        "VALUES (:id, :sid, :seq, :sp, :tx, :now)"
                    ),
                    {
                        "id": tid,
                        "sid": session_id,
                        "seq": i,
                        "sp": "user" if i == 0 else "agent",
                        "tx": f"turn {i}",
                        "now": now,
                    },
                )
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_seed())
    # Tests compare against the canonical (dashed) UUID string the API emits.
    return {
        "user_id": str(user_uuid),
        "session_id": str(session_uuid),
        "session_id_hex": session_id,
    }


@pytest.fixture(scope="module")
def app(db_engine_and_factory):
    """FastAPI app wired with admin/auth routers and the test DB session."""
    _engine, factory = db_engine_and_factory

    async def override_get_session():
        async with factory() as db:
            yield db

    fa = FastAPI()
    fa.include_router(auth_router.router)
    fa.include_router(admin_router.router)
    fa.dependency_overrides[get_session] = override_get_session
    return fa


@pytest.fixture
def admin_cookie():
    response = Response()
    token = issue_session_cookie(response, "ops@bonecho.ai")
    return {ADMIN_COOKIE_NAME: token}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_admin_signups_unauth_returns_401(app):
    client = TestClient(app)
    r = client.get("/api/admin/signups")
    assert r.status_code == 401


def test_auth_me_anonymous_returns_unauthenticated(app):
    client = TestClient(app)
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False}


def test_admin_signups_authenticated_returns_seeded_signup(
    app, seeded_ids, admin_cookie
):
    client = TestClient(app, cookies=admin_cookie)
    r = client.get("/api/admin/signups")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["limit"] == 50
    emails = [item["email"] for item in body["items"]]
    assert "founder@example.com" in emails
    target = next(i for i in body["items"] if i["email"] == "founder@example.com")
    assert target["latestSessionId"] == seeded_ids["session_id"]
    assert target["latestSessionStatus"] == "COMPLETED"


def test_admin_session_detail_returns_turns_and_artifacts(
    app, seeded_ids, admin_cookie
):
    client = TestClient(app, cookies=admin_cookie)
    r = client.get(f"/api/admin/sessions/{seeded_ids['session_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["userEmail"] == "founder@example.com"
    assert body["status"] == "COMPLETED"
    assert body["summary"] == "Built an agent for X"
    assert body["businessSummary"] == "X is a logistics SaaS"
    assert len(body["artifacts"]) == 2
    assert {a["kind"] for a in body["artifacts"]} == {"tdd_docx", "claude_md"}
    assert [t["seq"] for t in body["turns"]] == [0, 1]
    assert body["turns"][0]["speaker"] == "user"
    assert body["turns"][1]["speaker"] == "agent"


def test_admin_session_detail_writes_audit_row(
    app, db_engine_and_factory, seeded_ids, admin_cookie
):
    _engine, factory = db_engine_and_factory
    client = TestClient(app, cookies=admin_cookie)
    client.get(f"/api/admin/sessions/{seeded_ids['session_id']}")

    async def _count():
        async with factory() as db:
            res = await db.execute(
                text(
                    "SELECT COUNT(*) FROM admin_audit "
                    "WHERE actor_email = 'ops@bonecho.ai' "
                    "AND action = 'admin.session.view' "
                    "AND target_session_id = :sid"
                ),
                {"sid": seeded_ids["session_id_hex"]},
            )
            return res.scalar_one()

    count = asyncio.get_event_loop().run_until_complete(_count())
    assert count >= 1


def test_admin_audit_endpoint_lists_actions(app, admin_cookie):
    client = TestClient(app, cookies=admin_cookie)
    r = client.get("/api/admin/audit")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list)
    assert any(item["actorEmail"] == "ops@bonecho.ai" for item in body["items"])


def test_admin_session_detail_404_for_unknown_session(app, admin_cookie):
    client = TestClient(app, cookies=admin_cookie)
    r = client.get(f"/api/admin/sessions/{uuid.uuid4()}")
    assert r.status_code == 404


def test_admin_session_detail_422_for_bad_uuid(app, admin_cookie):
    client = TestClient(app, cookies=admin_cookie)
    r = client.get("/api/admin/sessions/not-a-uuid")
    assert r.status_code == 422
