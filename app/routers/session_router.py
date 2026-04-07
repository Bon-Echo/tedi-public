import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.session import Session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["session"])


@router.get("/session/{session_id}")
async def get_session_status(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict:
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid session ID format")

    result = await db.execute(select(Session).where(Session.id == session_uuid))
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": str(session.id),
        "status": session.status,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "created_at": session.created_at.isoformat(),
    }
