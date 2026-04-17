"""Admin dashboard JSON API. All routes require a valid admin session cookie.

This sits alongside the server-rendered HTML admin in `app/routers/admin.py`.
The HTML surface (`/admin/*`) is intended for direct internal access through an
upstream auth proxy; this JSON surface (`/api/admin/*`) is consumed by the SPA
admin dashboard and is gated by the in-app Google SSO + signed cookie.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.admin_auth import AdminPrincipal, require_admin
from app.models.session import Session as DBSession
from app.models.user import User
from app.schemas import (
    ManualFollowupRequest,
    ManualFollowupResponse,
)
from app.services import admin_query
from app.services.ondemand_followup import send_manual_followup

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-api"])


@router.get("/signups")
async def list_signups(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: AdminPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    items = await admin_query.list_signups(db, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/sessions/{session_id}")
async def get_session_detail(
    session_id: str,
    principal: AdminPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid session id") from exc

    detail = await admin_query.get_session_detail(db, session_uuid)
    if detail is None:
        raise HTTPException(status_code=404, detail="session not found")

    await admin_query.record_audit(
        db,
        actor_email=principal.email,
        action="admin.session.view",
        target_session_id=session_uuid,
    )
    await db.commit()
    return detail


@router.post("/sessions/{session_id}/followup", status_code=status.HTTP_200_OK)
async def post_manual_followup(
    session_id: str,
    body: ManualFollowupRequest,
    principal: AdminPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> ManualFollowupResponse:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid session id") from exc

    join_stmt = (
        select(DBSession, User)
        .join(User, User.id == DBSession.user_id)
        .where(DBSession.id == session_uuid)
    )
    row = (await db.execute(join_stmt)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    sess, user = row

    result = await send_manual_followup(
        db,
        session_uuid,
        recipient_email=user.email,
        body=body.body,
        subject=body.subject,
    )

    audit_row = await admin_query.record_audit(
        db,
        actor_email=principal.email,
        action="admin.session.followup",
        target_session_id=session_uuid,
        target_user_id=user.id,
        note=result.subject,
        metadata={"recipient": result.recipient},
    )
    await db.commit()

    return ManualFollowupResponse(
        ok=True,
        sentAt=result.sent_at,
        auditId=str(audit_row.id),
    )


@router.get("/audit")
async def list_audit(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    principal: AdminPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    items = await admin_query.list_audit(db, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}
