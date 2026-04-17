"""Read-side and audit helpers for the admin dashboard."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.admin_audit import AdminAudit
from app.models.session import Session as DBSession
from app.models.session_turn import SessionTurn
from app.models.user import User

logger = structlog.get_logger(__name__)


async def list_signups(
    db: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Return paginated signups joined with their most recent session."""
    latest_session_subq = (
        select(
            DBSession.user_id.label("user_id"),
            func.max(DBSession.created_at).label("latest_created_at"),
        )
        .group_by(DBSession.user_id)
        .subquery()
    )

    stmt = (
        select(User, DBSession)
        .join(
            latest_session_subq,
            latest_session_subq.c.user_id == User.id,
            isouter=True,
        )
        .join(
            DBSession,
            (DBSession.user_id == User.id)
            & (DBSession.created_at == latest_session_subq.c.latest_created_at),
            isouter=True,
        )
        .order_by(desc(User.created_at))
        .limit(limit)
        .offset(offset)
    )

    rows = (await db.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for user, sess in rows:
        out.append(
            {
                "userId": str(user.id),
                "email": user.email,
                "createdAt": user.created_at.isoformat(),
                "latestSessionId": str(sess.id) if sess else None,
                "latestSessionStatus": sess.status if sess else None,
                "latestSessionStartedAt": (
                    sess.started_at.isoformat() if sess and sess.started_at else None
                ),
                "latestSessionEndedAt": (
                    sess.ended_at.isoformat() if sess and sess.ended_at else None
                ),
            }
        )
    return out


async def get_session_detail(
    db: AsyncSession, session_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return full admin-side detail for a single session."""
    stmt = (
        select(DBSession, User)
        .join(User, User.id == DBSession.user_id)
        .where(DBSession.id == session_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    sess, user = row

    turns_stmt = (
        select(SessionTurn)
        .where(SessionTurn.session_id == session_id)
        .order_by(SessionTurn.seq.asc())
    )
    turns = (await db.execute(turns_stmt)).scalars().all()

    artifacts: list[dict[str, str]] = []
    if sess.tdd_s3_key:
        artifacts.append(
            {
                "kind": "tdd_docx",
                "s3Uri": f"s3://{settings.S3_BUCKET_NAME}/{sess.tdd_s3_key}",
            }
        )
    if sess.claude_md_s3_key:
        artifacts.append(
            {
                "kind": "claude_md",
                "s3Uri": f"s3://{settings.S3_BUCKET_NAME}/{sess.claude_md_s3_key}",
            }
        )

    return {
        "sessionId": str(sess.id),
        "userId": str(user.id),
        "userEmail": user.email,
        "status": sess.status,
        "startedAt": sess.started_at.isoformat() if sess.started_at else None,
        "endedAt": sess.ended_at.isoformat() if sess.ended_at else None,
        "createdAt": sess.created_at.isoformat(),
        "summary": sess.summary,
        "businessSummary": sess.business_summary,
        "transcriptS3Uri": sess.transcript_s3_uri,
        "artifacts": artifacts,
        "followupSentAt": (
            sess.followup_sent_at.isoformat() if sess.followup_sent_at else None
        ),
        "lastManualFollowupAt": (
            sess.last_manual_followup_at.isoformat()
            if sess.last_manual_followup_at
            else None
        ),
        "turns": [
            {
                "seq": t.seq,
                "speaker": t.speaker,
                "text": t.text,
                "createdAt": t.created_at.isoformat(),
            }
            for t in turns
        ],
    }


async def list_audit(
    db: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    stmt = (
        select(AdminAudit)
        .order_by(desc(AdminAudit.created_at))
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(row.id),
            "actorEmail": row.actor_email,
            "action": row.action,
            "targetSessionId": str(row.target_session_id)
            if row.target_session_id
            else None,
            "targetUserId": str(row.target_user_id) if row.target_user_id else None,
            "note": row.note,
            "metadata": row.metadata_json,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


async def record_audit(
    db: AsyncSession,
    *,
    actor_email: str,
    action: str,
    target_session_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AdminAudit:
    """Append a row to admin_audit. Caller is responsible for committing."""
    row = AdminAudit(
        actor_email=actor_email,
        action=action,
        target_session_id=target_session_id,
        target_user_id=target_user_id,
        note=note,
        metadata_json=metadata,
    )
    db.add(row)
    await db.flush()
    return row
