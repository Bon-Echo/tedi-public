"""WebSocket endpoint for browser audio streaming.

The browser client opens a WebSocket here to receive audio and send
barge-in / playback-finished signals.
"""

import uuid

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.services.browser import BrowserService
from app.services.session_service import SessionService
from app.session import RuntimeSessionState, TurnState

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["browser-ws"])

_session_service = SessionService()


async def _get_db() -> AsyncSession:
    async with async_session_factory() as db:
        yield db


@router.websocket("/api/v1/ws/{session_id}")
async def browser_websocket(
    websocket: WebSocket,
    session_id: uuid.UUID,
    token: uuid.UUID = Query(...),
) -> None:
    """WebSocket endpoint for the browser client.

    Auth: pass the session token as ?token=<session_token>.

    Messages from browser -> server:
        {"type": "ready"}
        {"type": "barge_in"}
        {"type": "playback_finished", "request_id": "..."}
        {"type": "speech_final", "transcript": "..."}

    Messages from server -> browser (via BrowserService):
        {"type": "thinking_start"}
        {"type": "response_start", "request_id": "...", "spoken_text": "..."}
        {"type": "audio_chunk", "request_id": "...", "audio_base64": "...", "is_final": false}
        {"type": "response_complete", "request_id": "..."}
        {"type": "stop_playback"}
    """
    browser_service: BrowserService = websocket.app.state.browser_service
    sid = str(session_id)

    async with async_session_factory() as db:
        db_session = await _session_service.get_session_by_token(token, db)

        if db_session is None or db_session.id != session_id:
            logger.warning("browser_ws_invalid_token", session_id=sid)
            await websocket.close(code=4003, reason="Forbidden")
            return

        if db_session.status not in ("CREATED", "ACTIVE"):
            logger.warning(
                "browser_ws_invalid_status",
                session_id=sid,
                status=db_session.status,
            )
            await websocket.close(code=4003, reason="Forbidden")
            return

        await websocket.accept()

        # Transition CREATED -> ACTIVE on first connect
        if db_session.status == "CREATED":
            await _session_service.transition_to_active(db_session, db)

        runtime_state = RuntimeSessionState(session_id=session_id)
        await browser_service.register(sid, websocket)

        try:
            while True:
                data = await websocket.receive_json()
                msg_type = data.get("type")

                if msg_type == "ready":
                    logger.info("browser_ready", session_id=sid)

                elif msg_type == "barge_in":
                    logger.info("browser_barge_in", session_id=sid)
                    await browser_service.cancel_request(sid)
                    browser_service.mark_playback_finished(sid)
                    async with runtime_state.lock:
                        runtime_state.turn_state = TurnState.IDLE
                        runtime_state.active_request_id = None

                elif msg_type == "playback_finished":
                    request_id = data.get("request_id")
                    logger.info(
                        "browser_playback_finished",
                        session_id=sid,
                        request_id=request_id,
                    )
                    browser_service.mark_playback_finished(sid)
                    async with runtime_state.lock:
                        if runtime_state.active_request_id == request_id:
                            runtime_state.turn_state = TurnState.IDLE

                elif msg_type == "speech_final":
                    transcript_text = data.get("transcript", "")
                    logger.info(
                        "browser_speech_final",
                        session_id=sid,
                        transcript_len=len(transcript_text),
                    )
                    async with runtime_state.lock:
                        if transcript_text:
                            runtime_state.transcript.append(
                                {"role": "user", "text": transcript_text}
                            )
                        runtime_state.turn_state = TurnState.TURN_RECEIVED

                else:
                    logger.debug(
                        "browser_ws_unknown_message",
                        session_id=sid,
                        msg_type=msg_type,
                    )

        except WebSocketDisconnect:
            logger.info("browser_ws_disconnected", session_id=sid)
        except Exception:
            logger.exception("browser_ws_error", session_id=sid)
        finally:
            # Cancel any pending timeout task (wired up by orchestrator in BON-21)
            if runtime_state.timeout_task is not None:
                runtime_state.timeout_task.cancel()

            await browser_service.unregister(sid)

            # Transition session to ENDED
            async with async_session_factory() as end_db:
                end_session = await _session_service.get_session_by_token(token, end_db)
                if end_session is not None and end_session.status == "ACTIVE":
                    await _session_service.transition_to_ended(end_session, end_db)
