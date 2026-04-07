"""WebSocket endpoint — /ws/bot/{call_id}."""

import asyncio

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.session import Session as DBSession
from app.models.user import User
from app.session import SessionState, TurnState

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])


async def _lookup_user_email(call_id: str) -> str | None:
    """Look up the user email for a session from the database."""
    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(User.email)
                .join(DBSession, DBSession.user_id == User.id)
                .where(DBSession.id == call_id)
            )
            row = result.scalar_one_or_none()
            return row
    except Exception:
        logger.exception("user_email_lookup_failed", call_id=call_id)
        return None


@router.websocket("/ws/bot/{call_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    call_id: str,
) -> None:
    """WebSocket endpoint that room.js connects to.

    call_id is the DB session UUID returned by POST /api/v1/signup in roomUrl.
    It is used as the in-memory session key so orchestrator lookups resolve correctly.
    """
    orchestrator = websocket.app.state.orchestrator
    browser_service = websocket.app.state.browser_service
    session_manager = websocket.app.state.session_manager

    await websocket.accept()

    # Get or create the in-memory session, keyed by call_id.
    session = session_manager.get_session(call_id)
    if session is None:
        session = SessionState()
        session.session_id = call_id
        session_manager._sessions[call_id] = session

    session_id = call_id

    await browser_service.register(session_id, websocket)

    # Look up user email for post-session delivery
    user_email = await _lookup_user_email(call_id)
    if user_email:
        orchestrator.set_user_email(session_id, user_email)
        logger.info("ws_user_email_resolved", session_id=session_id, email=user_email)

    logger.info("ws_session_started", session_id=session_id)

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break

            msg_type = data.get("type")

            if msg_type == "ready":
                logger.info("ws_client_ready", session_id=session_id)

            elif msg_type == "barge_in":
                logger.info("ws_barge_in", session_id=session_id)
                await browser_service.cancel_request(session_id)
                async with session.lock:
                    session.turn_state = TurnState.IDLE

            elif msg_type == "playback_finished":
                logger.info("ws_playback_finished", session_id=session_id)
                browser_service.mark_playback_finished(session_id)
                async with session.lock:
                    session.turn_state = TurnState.IDLE

            elif msg_type == "speech_final":
                transcript = data.get("transcript", "").strip()
                if transcript:
                    logger.info(
                        "ws_speech_final",
                        session_id=session_id,
                        transcript=transcript[:80],
                    )
                    asyncio.create_task(
                        orchestrator.on_speech_final(session_id, transcript)
                    )

            else:
                logger.debug(
                    "ws_unknown_message_type",
                    session_id=session_id,
                    msg_type=msg_type,
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("ws_error", session_id=session_id)
    finally:
        await browser_service.unregister(session_id)
        session_manager.remove_session(session_id)
        logger.info("ws_session_ended", session_id=session_id)
