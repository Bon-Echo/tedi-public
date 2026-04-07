"""Active discovery orchestrator — Tedi drives conversation, no gate check."""

import asyncio

import structlog

from app.schemas import DiscoveryResponse, DiscoveryUpdate, SessionPhase
from app.services.claude import ClaudeService
from app.services.elevenlabs import ElevenLabsService
from app.session import (
    CONVERSATION_HISTORY_WINDOW,
    SessionManager,
    SessionPhase as SessionPhaseEnum,
    SessionState,
    SessionStatus,
    TurnState,
)

logger = structlog.get_logger(__name__)


class Orchestrator:
    """Active discovery pipeline: user speech -> Claude -> ElevenLabs TTS -> playback.

    Key differences from passive-listener Tedi:
    - No gate check — every user utterance triggers a Claude response
    - Elapsed time is injected into every Claude call for session pacing
    - Session phase auto-transitions based on elapsed time
    - On CLOSING phase or session timeout, post-session pipeline fires
    """

    def __init__(
        self,
        session_manager: SessionManager,
        claude_service: ClaudeService,
        elevenlabs_service: ElevenLabsService,
        post_session_service: object | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._claude = claude_service
        self._elevenlabs = elevenlabs_service
        self._post_session = post_session_service

    async def on_speech_final(self, session_id: str, text: str) -> None:
        """Callback when final transcribed user speech arrives.

        Every utterance triggers Tedi's response — there is no gate check.
        """
        session = self._session_manager.get_session(session_id)
        if session is None:
            logger.warning("speech_final_session_not_found", session_id=session_id)
            return

        if not text.strip():
            return

        async with session.lock:
            session.transcript.append({"speaker": "user", "text": text, "is_final": True})
            session.conversation_history.append({"role": "user", "content": text})
            # Update phase before processing so Claude sees current phase
            session.update_phase()

        # Drop turn if already processing or speaking
        async with session.lock:
            if session.turn_state in (TurnState.PROCESSING, TurnState.SPEAKING):
                logger.info(
                    "speech_final_dropped_already_responding",
                    session_id=session_id,
                    turn_state=session.turn_state.value,
                )
                return
            session.turn_state = TurnState.PROCESSING

        logger.info(
            "speech_final_turn_triggered",
            session_id=session_id,
            text=text[:80],
            phase=session.session_phase.value,
            elapsed_minutes=round(session.elapsed_minutes(), 1),
        )

        await self._process_turn(session_id)

    async def _process_turn(self, session_id: str) -> None:
        """Core turn: inject elapsed time -> Claude -> apply discovery updates -> TTS -> playback."""
        session = self._session_manager.get_session(session_id)
        if session is None:
            return

        try:
            async with session.lock:
                session.status = SessionStatus.PROCESSING

            elapsed = session.elapsed_minutes()

            windowed_history = _get_windowed_history(
                session.conversation_history,
                CONVERSATION_HISTORY_WINDOW,
            )

            discovery_context = {
                "discovery_sections": session.discovery_sections,
                "coverage": session.coverage,
            }

            response: DiscoveryResponse = await self._claude.generate_response(
                conversation_history=windowed_history,
                discovery_context=discovery_context,
                elapsed_minutes=elapsed,
            )

            _apply_discovery_updates(session, response.discovery_updates)
            _apply_coverage(session, response.coverage)

            await self._on_discovery_updated(session_id, session)

            if not response.spoken_response.strip():
                logger.info("turn_silent", session_id=session_id)
                async with session.lock:
                    session.status = SessionStatus.ACTIVE
                    session.turn_state = TurnState.IDLE
                return

            async with session.lock:
                session.conversation_history.append(
                    {"role": "assistant", "content": response.spoken_response}
                )
                session.status = SessionStatus.SPEAKING
                session.turn_state = TurnState.SPEAKING

            logger.info(
                "turn_response_ready",
                session_id=session_id,
                response_length=len(response.spoken_response),
                discovery_updates=len(response.discovery_updates),
                phase=response.session_phase.value,
                elapsed_minutes=round(elapsed, 1),
            )

            await self._synthesize_and_play(session_id, session, response.spoken_response)

            # Check if session should end
            if response.session_phase == SessionPhase.CLOSING or _is_session_timeout(session):
                await self._end_session(session_id, session)
                return

            async with session.lock:
                session.status = SessionStatus.ACTIVE
                session.turn_state = TurnState.IDLE

            logger.info("turn_completed", session_id=session_id)

        except Exception:
            logger.exception("turn_processing_failed", session_id=session_id)
            async with session.lock:
                session.status = SessionStatus.ACTIVE
                session.turn_state = TurnState.IDLE

    async def _synthesize_and_play(
        self, session_id: str, session: SessionState, text: str
    ) -> None:
        """Stream ElevenLabs TTS audio for playback.

        Subclasses or integration layers should override or wrap this method
        to route audio to the appropriate playback channel (WebSocket, Recall.ai, etc.).
        """
        request_id = str(id(text))
        async with session.lock:
            session.active_request_id = request_id

        chunk_count = 0
        async for _chunk in self._elevenlabs.text_to_speech_streamed(text):
            # Barge-in check — concrete integrations set cancellation flag
            if self._is_cancelled(session_id):
                logger.info(
                    "synthesis_cancelled_barge_in",
                    session_id=session_id,
                    chunks_sent=chunk_count,
                )
                return
            await self._deliver_audio_chunk(session_id, _chunk)
            chunk_count += 1

        logger.info(
            "synthesis_complete",
            session_id=session_id,
            chunks_sent=chunk_count,
        )

    def _is_cancelled(self, session_id: str) -> bool:
        """Return True if barge-in cancellation has been signalled for this session.

        Concrete integrations override this to check their own cancellation flag.
        """
        return False

    async def _on_discovery_updated(self, session_id: str, session: SessionState) -> None:
        """Hook called after discovery sections are updated. Override to push to clients."""

    async def _deliver_audio_chunk(self, session_id: str, chunk: bytes) -> None:
        """Deliver a single audio chunk to the playback channel.

        Concrete integrations override this to route to WebSocket, Recall.ai, etc.
        """

    async def _end_session(self, session_id: str, session: SessionState) -> None:
        """Mark session as post-session and fire the output pipeline."""
        async with session.lock:
            session.status = SessionStatus.POST_SESSION
            session.turn_state = TurnState.IDLE

        logger.info(
            "session_ending",
            session_id=session_id,
            elapsed_minutes=round(session.elapsed_minutes(), 1),
            coverage=session.coverage,
        )

        if self._post_session is not None:
            asyncio.create_task(self._post_session.process(session))  # type: ignore[attr-defined]

    async def handle_session_timeout(self, session_id: str) -> None:
        """Force session end on external timeout signal."""
        session = self._session_manager.get_session(session_id)
        if session is None:
            logger.warning("timeout_session_not_found", session_id=session_id)
            return
        logger.info("session_timeout", session_id=session_id)
        await self._end_session(session_id, session)


# --- Helpers ---

def _get_windowed_history(
    history: list[dict[str, str]],
    window: int,
) -> list[dict[str, str]]:
    """Return at most `window` most recent messages, starting from a user turn."""
    if len(history) <= window:
        return history
    sliced = history[-window:]
    for i, msg in enumerate(sliced):
        if msg.get("role") == "user":
            return sliced[i:]
    return sliced


def _apply_discovery_updates(
    session: SessionState, updates: list[DiscoveryUpdate]
) -> None:
    """Apply discovery section updates to session state."""
    for update in updates:
        area = update.area.value
        content = update.content
        if update.action == "append":
            existing = session.discovery_sections.get(area, "")
            session.discovery_sections[area] = (
                f"{existing}\n{content}" if existing else content
            )
        elif update.action == "replace":
            session.discovery_sections[area] = content
        else:
            logger.warning(
                "unknown_discovery_update_action",
                action=update.action,
                area=area,
            )


def _apply_coverage(session: SessionState, coverage: object) -> None:
    """Merge coverage values from Claude response into session state.

    Only increases coverage — never decreases (Claude may occasionally under-report).
    """
    if coverage is None:
        return
    coverage_dict = coverage.to_dict() if hasattr(coverage, "to_dict") else {}
    for area, value in coverage_dict.items():
        if area in session.coverage:
            session.coverage[area] = max(session.coverage[area], value)


def _is_session_timeout(session: SessionState, max_minutes: float = 12.0) -> bool:
    """Return True if the session has exceeded the hard timeout."""
    return session.elapsed_minutes() >= max_minutes
