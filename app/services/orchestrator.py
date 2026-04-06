"""Orchestrator: wires speech_final → Claude → ElevenLabs TTS → browser audio."""

import asyncio
import uuid
from typing import TYPE_CHECKING

import structlog

from app.config import settings
from app.schemas import ClaudeResponse, TDDUpdate
from app.session import RuntimeSessionState, TurnState

if TYPE_CHECKING:
    from app.services.browser import BrowserService
    from app.services.claude import ClaudeService
    from app.services.elevenlabs import ElevenLabsService
    from app.services.session_service import SessionService

logger = structlog.get_logger(__name__)


class Orchestrator:
    """Central pipeline: speech_final → gating → Claude → TDD updates → ElevenLabs → audio_chunk → browser.

    Handles:
    - Turn-guard to prevent overlapping processing
    - Haiku gating to decide when Tedi should respond
    - Session timeout with 2-minute warning and graceful goodbye
    - Barge-in: BrowserService cancel events interrupt _synthesize_to_browser mid-stream
    """

    def __init__(
        self,
        browser_service: "BrowserService",
        claude_service: "ClaudeService",
        elevenlabs_service: "ElevenLabsService",
        session_service: "SessionService",
    ) -> None:
        self._browser = browser_service
        self._claude = claude_service
        self._elevenlabs = elevenlabs_service
        self._session_service = session_service

    async def on_speech_final(
        self,
        session_id: str,
        text: str,
        runtime_state: RuntimeSessionState,
    ) -> None:
        """Handle a final speech transcript from the browser.

        Always accumulates transcript for passive note-taking.
        Only triggers Claude when the speaker addresses Tedi by name.
        """
        if not text.strip():
            return

        # Accumulate transcript and conversation history
        async with runtime_state.lock:
            runtime_state.transcript.append({"role": "user", "text": text})
            runtime_state.conversation_history.append(
                {"role": "user", "content": text}
            )

        # Haiku gating: cheap, fast check — is Tedi being addressed?
        recent_lines = [
            entry.get("content", "")
            for entry in runtime_state.conversation_history[-5:]
            if entry.get("role") == "user"
        ]
        should_respond = await self._claude.should_respond(recent_lines)

        if not should_respond:
            logger.info(
                "speech_final_passive",
                session_id=session_id,
                text=text[:80],
            )
            return

        # Turn guard: drop if already processing or speaking
        async with runtime_state.lock:
            if runtime_state.turn_state in (TurnState.PROCESSING, TurnState.SPEAKING):
                logger.info(
                    "speech_final_dropped_already_responding",
                    session_id=session_id,
                    turn_state=runtime_state.turn_state.value,
                )
                return
            runtime_state.turn_state = TurnState.PROCESSING

        logger.info(
            "speech_final_turn_triggered",
            session_id=session_id,
            text=text[:80],
        )

        await self._process_turn(session_id, text, runtime_state)

    async def _process_turn(
        self,
        session_id: str,
        text: str,
        runtime_state: RuntimeSessionState,
    ) -> None:
        """Core turn: Claude reasoning → TDD updates → ElevenLabs TTS → browser audio."""
        try:
            # Signal the browser that Tedi is thinking
            await self._browser.send_thinking_start(session_id)

            # Sliding window of recent history — TDD sections capture earlier context
            windowed_history = _get_windowed_history(
                runtime_state.conversation_history,
                settings.CONVERSATION_HISTORY_WINDOW,
            )

            claude_response: ClaudeResponse = await self._claude.generate_response(
                conversation_history=windowed_history,
                tdd_context=runtime_state.tdd_sections,
            )

            _apply_tdd_updates(runtime_state, claude_response.tdd_updates)

            # If Claude chose not to speak, return to idle
            if not claude_response.spoken_response.strip():
                logger.info(
                    "turn_silent",
                    session_id=session_id,
                    tdd_updates=len(claude_response.tdd_updates),
                )
                async with runtime_state.lock:
                    runtime_state.turn_state = TurnState.IDLE
                return

            async with runtime_state.lock:
                runtime_state.conversation_history.append(
                    {"role": "assistant", "content": claude_response.spoken_response}
                )
                runtime_state.turn_state = TurnState.SPEAKING

            logger.info(
                "turn_response_ready",
                session_id=session_id,
                response_length=len(claude_response.spoken_response),
                tdd_updates=len(claude_response.tdd_updates),
            )

            await self._synthesize_to_browser(
                session_id, runtime_state, claude_response.spoken_response
            )

            if claude_response.should_leave:
                logger.info("should_leave_triggered", session_id=session_id)
                await self._browser.wait_for_playback(session_id, timeout=30.0)
                await self._browser.send_message(session_id, {"type": "session_ended"})
                await self._browser.close_connection(session_id, code=1000, reason="session_ended")

            # turn_state reset to IDLE happens when browser sends playback_finished

        except Exception:
            logger.exception("turn_processing_failed", session_id=session_id)
            async with runtime_state.lock:
                runtime_state.turn_state = TurnState.IDLE

    async def _synthesize_to_browser(
        self,
        session_id: str,
        runtime_state: RuntimeSessionState,
        text: str,
    ) -> None:
        """Stream ElevenLabs TTS audio chunks to the browser.

        Supports barge-in: if BrowserService cancellation is set mid-stream,
        stops sending and notifies the browser to stop playback.
        """
        request_id = self._browser.new_request_id()
        async with runtime_state.lock:
            runtime_state.active_request_id = request_id

        self._browser.reset_cancellation(session_id)
        await self._browser.send_response_start(session_id, request_id, text)

        logger.info(
            "media_synthesis_started",
            session_id=session_id,
            request_id=request_id,
            text_length=len(text),
        )

        chunk_count = 0
        async for chunk in self._elevenlabs.text_to_speech_streamed(text):
            if self._browser.is_cancelled(session_id):
                logger.info(
                    "media_synthesis_cancelled_barge_in",
                    session_id=session_id,
                    request_id=request_id,
                    chunks_sent=chunk_count,
                )
                await self._browser.send_stop_playback(session_id)
                return

            sent = await self._browser.send_audio_chunk(
                session_id=session_id,
                request_id=request_id,
                audio_chunk=chunk,
                is_final=False,
            )
            if not sent:
                logger.warning(
                    "media_synthesis_send_failed",
                    session_id=session_id,
                    request_id=request_id,
                )
                break
            chunk_count += 1

        if not self._browser.is_cancelled(session_id):
            await self._browser.send_response_complete(session_id, request_id)
            logger.info(
                "media_synthesis_complete",
                session_id=session_id,
                request_id=request_id,
                chunks_sent=chunk_count,
            )

    def start_session_timeout(
        self,
        session_id: str,
        runtime_state: RuntimeSessionState,
        session_token: uuid.UUID,
    ) -> None:
        """Schedule the session timeout task and store handle on runtime_state."""
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_session_timeout(session_id, runtime_state, session_token)
        )
        runtime_state.timeout_task = task
        logger.info(
            "session_timeout_scheduled",
            session_id=session_id,
            timeout_seconds=settings.SESSION_TIMEOUT_SECONDS,
        )

    async def _run_session_timeout(
        self,
        session_id: str,
        runtime_state: RuntimeSessionState,
        session_token: uuid.UUID,
    ) -> None:
        """Run the session timeout: warn at T-2min, send goodbye at T, close session."""
        timeout = settings.SESSION_TIMEOUT_SECONDS
        warning_before = 120  # 2 minutes

        try:
            # Sleep until warning time
            await asyncio.sleep(max(0, timeout - warning_before))

            # Send session_ending warning to browser
            await self._browser.send_message(
                session_id,
                {"type": "session_ending", "seconds_remaining": warning_before},
            )
            logger.info("session_ending_warning_sent", session_id=session_id)

            # Sleep remaining 2 minutes
            await asyncio.sleep(warning_before)

        except asyncio.CancelledError:
            logger.info("session_timeout_cancelled", session_id=session_id)
            return

        # Timeout reached — send goodbye
        logger.info("session_timeout_reached", session_id=session_id)

        try:
            async with runtime_state.lock:
                # Don't interrupt an active turn — wait a moment
                pass

            goodbye_text = (
                "Thank you so much for including me today. "
                "I've captured everything for your Technical Design Document. "
                "Our session time is up — I'll leave you to it. Goodbye!"
            )
            await self._browser.send_thinking_start(session_id)
            await self._synthesize_to_browser(session_id, runtime_state, goodbye_text)
            await self._browser.wait_for_playback(session_id, timeout=15.0)
        except Exception:
            logger.exception("session_timeout_goodbye_failed", session_id=session_id)

        # Notify browser session has ended
        await self._browser.send_message(session_id, {"type": "session_ended"})

        # Transition DB session to TIMED_OUT
        try:
            from app.database import async_session_factory

            async with async_session_factory() as db:
                session = await self._session_service.get_session_by_token(
                    session_token, db
                )
                if session is not None and session.status == "ACTIVE":
                    await self._session_service.transition_to_timed_out(session, db)
        except Exception:
            logger.exception("session_timeout_db_transition_failed", session_id=session_id)

        # Close WebSocket connection
        await self._browser.close_connection(session_id, code=1000, reason="session_timeout")


def _get_windowed_history(
    history: list[dict[str, str]],
    window: int,
) -> list[dict[str, str]]:
    """Return at most `window` most recent messages, starting from a user turn.

    Anthropic requires the first message to have role "user". If the slice
    starts on an assistant turn, advance until a user message is found.
    """
    if len(history) <= window:
        return history
    sliced = history[-window:]
    for i, msg in enumerate(sliced):
        if msg.get("role") == "user":
            return sliced[i:]
    return sliced


def _apply_tdd_updates(
    runtime_state: RuntimeSessionState,
    updates: list[TDDUpdate],
) -> None:
    """Apply TDD section updates to runtime session state."""
    for update in updates:
        section = update.section
        content = update.content

        if update.action == "append":
            existing = runtime_state.tdd_sections.get(section, "")
            if isinstance(existing, str):
                runtime_state.tdd_sections[section] = (
                    f"{existing}\n{content}" if existing else content
                )
            elif isinstance(existing, list):
                runtime_state.tdd_sections[section].append(content)
            else:
                runtime_state.tdd_sections[section] = content
        elif update.action == "replace":
            runtime_state.tdd_sections[section] = content
        else:
            logger.warning(
                "unknown_tdd_update_action",
                action=update.action,
                section=section,
            )
