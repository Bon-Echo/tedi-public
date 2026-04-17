"""Admin dashboard routes.

Server-rendered admin pages for reviewing signups and conversation details.
These routes deliberately contain NO authentication — the backend is
expected to enforce @bonecho.ai SSO (e.g. via an upstream proxy, ALB auth
action, or middleware) around every path mounted under /admin.

The pages only READ from existing models/services — they do not mutate
state — so the gating concern stays fully in the backend layer.

The `get_admin_user` dependency surfaces the authenticated identity when
the backend forwards it via a standard header (e.g. X-Forwarded-Email set
by an OIDC-aware proxy). When the header is absent the dependency falls
back to "admin" so the UI still renders in local development.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.session import Session as DBSession
from app.models.user import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Auth dependency — placeholder for backend-owned @bonecho.ai SSO
# ---------------------------------------------------------------------------


def get_admin_user(request: Request) -> str:
    """Resolve the authenticated admin identity.

    The backend is responsible for enforcing @bonecho.ai SSO in front of
    every /admin route (ALB auth action, oauth2-proxy, middleware, etc.).
    When the upstream auth layer is present it is expected to forward the
    verified identity via one of the standard headers below.

    If none are present — as in local development or an accidentally
    unprotected deployment — we fall back to "admin" so the UI still
    renders. The backend auth layer is the source of truth for access.
    """
    forwarded = (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Auth-Request-Email")
        or request.headers.get("X-User-Email")
    )
    if forwarded:
        return forwarded
    return "admin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _status_class(status: str) -> str:
    s = (status or "").upper()
    if s in ("COMPLETED", "ACTIVE"):
        return "ok"
    if s in ("ENDED", "POST_PROCESSING"):
        return "muted"
    if s in ("TIMED_OUT", "ERROR"):
        return "warn"
    return "muted"


def _live_session_transcript(request: Request, session_id: str) -> list[dict[str, Any]]:
    """Read an in-memory session transcript (live + still connected)."""
    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        return []
    sess = sm.get_session(session_id)
    if sess is None:
        return []
    return list(sess.transcript or [])


def _live_session_summary(request: Request, session_id: str) -> dict[str, Any] | None:
    sm = getattr(request.app.state, "session_manager", None)
    if sm is None:
        return None
    sess = sm.get_session(session_id)
    if sess is None:
        return None
    return {
        "discovery_sections": dict(sess.discovery_sections),
        "coverage": dict(sess.coverage),
        "phase": sess.session_phase.value,
        "status": sess.status.value,
        "turn_state": sess.turn_state.value,
        "elapsed_minutes": round(sess.elapsed_minutes(), 1),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    admin: str = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Dashboard home — signup volume + live session count."""
    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    total_sessions = (await db.execute(select(func.count(DBSession.id)))).scalar_one()
    today_sessions = (
        await db.execute(
            select(func.count(DBSession.id)).where(
                func.date(DBSession.created_at) == func.current_date()
            )
        )
    ).scalar_one()

    status_rows = (
        await db.execute(
            select(DBSession.status, func.count(DBSession.id)).group_by(DBSession.status)
        )
    ).all()
    status_breakdown = {row[0]: row[1] for row in status_rows}

    recent_signups_res = await db.execute(
        select(User).order_by(desc(User.created_at)).limit(10)
    )
    recent_signups = recent_signups_res.scalars().all()

    recent_sessions_res = await db.execute(
        select(DBSession, User)
        .join(User, User.id == DBSession.user_id)
        .order_by(desc(DBSession.created_at))
        .limit(10)
    )
    recent_sessions = [
        {
            "id": str(s.id),
            "status": s.status,
            "status_class": _status_class(s.status),
            "created_at": _fmt_dt(s.created_at),
            "started_at": _fmt_dt(s.started_at),
            "ended_at": _fmt_dt(s.ended_at),
            "email": u.email,
        }
        for s, u in recent_sessions_res.all()
    ]

    sm = getattr(request.app.state, "session_manager", None)
    live_sessions = sm.list_sessions() if sm else []

    return templates.TemplateResponse(
        request,
        "admin/home.html",
        {
            "admin_email": admin,
            "metrics": {
                "total_users": total_users,
                "total_sessions": total_sessions,
                "today_sessions": today_sessions,
                "live_sessions": len(live_sessions),
            },
            "status_breakdown": status_breakdown,
            "recent_signups": [
                {
                    "id": str(u.id),
                    "email": u.email,
                    "created_at": _fmt_dt(u.created_at),
                }
                for u in recent_signups
            ],
            "recent_sessions": recent_sessions,
            "live_sessions": live_sessions,
        },
    )


@router.get("/signups", response_class=HTMLResponse)
async def admin_signups(
    request: Request,
    admin: str = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
    q: str | None = Query(default=None, description="Email search"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=5, le=200),
) -> HTMLResponse:
    """Paginated signup list with optional email search."""
    stmt = select(User, func.count(DBSession.id).label("session_count")).outerjoin(
        DBSession, DBSession.user_id == User.id
    ).group_by(User.id)

    count_stmt = select(func.count(User.id))

    if q:
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.email).like(pattern))
        count_stmt = count_stmt.where(func.lower(User.email).like(pattern))

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(desc(User.created_at)).limit(per_page).offset((page - 1) * per_page)

    rows = (await db.execute(stmt)).all()
    signups = [
        {
            "id": str(u.id),
            "email": u.email,
            "created_at": _fmt_dt(u.created_at),
            "session_count": sess_count or 0,
        }
        for u, sess_count in rows
    ]

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(
        request,
        "admin/signups.html",
        {
            "admin_email": admin,
            "signups": signups,
            "q": q or "",
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    )


@router.get("/signups.csv")
async def admin_signups_csv(
    admin: str = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Export all signups as CSV."""
    rows = (
        await db.execute(
            select(User.email, User.created_at).order_by(desc(User.created_at))
        )
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "created_at_utc"])
    for email, created_at in rows:
        writer.writerow([email, created_at.astimezone(timezone.utc).isoformat()])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tedi-signups.csv"'},
    )


@router.get("/conversations", response_class=HTMLResponse)
async def admin_conversations(
    request: Request,
    admin: str = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Email search"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=5, le=200),
) -> HTMLResponse:
    """Paginated conversation list with optional status + email filters."""
    stmt = (
        select(DBSession, User)
        .join(User, User.id == DBSession.user_id)
    )
    count_stmt = select(func.count(DBSession.id)).join(User, User.id == DBSession.user_id)

    if status:
        stmt = stmt.where(DBSession.status == status)
        count_stmt = count_stmt.where(DBSession.status == status)
    if q:
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.email).like(pattern))
        count_stmt = count_stmt.where(func.lower(User.email).like(pattern))

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = (
        stmt.order_by(desc(DBSession.created_at))
        .limit(per_page)
        .offset((page - 1) * per_page)
    )

    rows = (await db.execute(stmt)).all()
    sm = getattr(request.app.state, "session_manager", None)
    live_ids = set()
    if sm:
        live_ids = {s["session_id"] for s in sm.list_sessions()}

    conversations = [
        {
            "id": str(s.id),
            "email": u.email,
            "status": s.status,
            "status_class": _status_class(s.status),
            "created_at": _fmt_dt(s.created_at),
            "started_at": _fmt_dt(s.started_at),
            "ended_at": _fmt_dt(s.ended_at),
            "is_live": str(s.id) in live_ids,
            "has_transcript": bool(s.transcript_s3_uri),
        }
        for s, u in rows
    ]

    total_pages = max(1, (total + per_page - 1) // per_page)

    return templates.TemplateResponse(
        request,
        "admin/conversations.html",
        {
            "admin_email": admin,
            "conversations": conversations,
            "q": q or "",
            "status": status or "",
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "status_choices": [
                "CREATED",
                "ACTIVE",
                "ENDED",
                "TIMED_OUT",
                "POST_PROCESSING",
                "COMPLETED",
                "ERROR",
            ],
        },
    )


@router.get("/conversations/{session_id}", response_class=HTMLResponse)
async def admin_conversation_detail(
    session_id: str,
    request: Request,
    admin: str = Depends(get_admin_user),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Single-conversation detail view."""
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session ID format")

    row = (
        await db.execute(
            select(DBSession, User)
            .join(User, User.id == DBSession.user_id)
            .where(DBSession.id == session_uuid)
        )
    ).first()

    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    db_session, user = row

    transcript = _live_session_transcript(request, session_id)
    live_summary = _live_session_summary(request, session_id)
    is_live = live_summary is not None

    duration_s: int | None = None
    if db_session.started_at and db_session.ended_at:
        duration_s = int(
            (db_session.ended_at - db_session.started_at).total_seconds()
        )

    return templates.TemplateResponse(
        request,
        "admin/conversation_detail.html",
        {
            "admin_email": admin,
            "conversation": {
                "id": str(db_session.id),
                "email": user.email,
                "user_id": str(user.id),
                "status": db_session.status,
                "status_class": _status_class(db_session.status),
                "created_at": _fmt_dt(db_session.created_at),
                "started_at": _fmt_dt(db_session.started_at),
                "ended_at": _fmt_dt(db_session.ended_at),
                "duration_s": duration_s,
                "transcript_s3_uri": db_session.transcript_s3_uri,
                "token": str(db_session.token),
            },
            "is_live": is_live,
            "live_summary": live_summary,
            "transcript": transcript,
            "transcript_json": json.dumps(transcript, indent=2) if transcript else "",
        },
    )
