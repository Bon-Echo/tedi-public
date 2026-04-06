import uuid
from datetime import timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models.session import Session
from app.schemas import SessionResponse

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["session"])


@router.get("/session/{session_id}", response_model=SessionResponse)
async def get_session_status(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    timeout_at = None
    if session.started_at is not None:
        timeout_at = session.started_at + timedelta(seconds=settings.SESSION_TIMEOUT_SECONDS)

    return JSONResponse(
        content=SessionResponse(
            id=session.id,
            status=session.status,
            startedAt=session.started_at,
            timeoutAt=timeout_at,
        ).model_dump(mode="json"),
    )
