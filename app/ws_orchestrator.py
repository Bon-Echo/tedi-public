"""WebSocketOrchestrator — bridges Orchestrator to BrowserService for browser sessions."""

import asyncio
import copy

import structlog

from app.orchestrator import Orchestrator
from app.services.browser import BrowserService
from app.services.claude import ClaudeService
from app.services.elevenlabs import ElevenLabsService
from app.services.post_session import run_post_session_pipeline
from app.session import SessionManager, SessionState

logger = structlog.get_logger(__name__)


class WebSocketOrchestrator(Orchestrator):
    """Orchestrator subclass that routes audio and control messages to a browser WebSocket."""

    def __init__(
        self,
        browser_service: BrowserService,
        session_manager: SessionManager,
        claude_service: ClaudeService,
        elevenlabs_service: ElevenLabsService,
        post_session_service: object | None = None,
    ) -> None:
        super().__init__(
            session_manager=session_manager,
            claude_service=claude_service,
            elevenlabs_service=elevenlabs_service,
            post_session_service=post_session_service,
        )
        self._browser = browser_service
        self._request_ids: dict[str, str] = {}
        # user email per session for post-session email delivery
        self._user_emails: dict[str, str] = {}

    def set_user_email(self, session_id: str, email: str) -> None:
        """Store user email for post-session delivery."""
        self._user_emails[session_id] = email

    async def on_speech_final(self, session_id: str, text: str) -> None:
        """Send thinking_start before delegating to the base turn pipeline."""
        await self._browser.send_thinking_start(session_id)
        await super().on_speech_final(session_id, text)

    def _is_cancelled(self, session_id: str) -> bool:
        return self._browser.is_cancelled(session_id)

    async def _on_discovery_updated(self, session_id: str, session: SessionState) -> None:
        """Push discovery updates to browser for the executive summary panel."""
        await self._browser.send_discovery_update(
            session_id, session.discovery_sections, session.coverage
        )

    async def _deliver_audio_chunk(self, session_id: str, chunk: bytes) -> None:
        request_id = self._request_ids.get(session_id, "")
        await self._browser.send_audio_chunk(session_id, request_id, chunk)

    async def _synthesize_and_play(
        self, session_id: str, session: SessionState, text: str
    ) -> None:
        request_id = self._browser.new_request_id()
        self._request_ids[session_id] = request_id
        self._browser.reset_cancellation(session_id)
        await self._browser.send_response_start(session_id, request_id, text)
        await super()._synthesize_and_play(session_id, session, text)
        if not self._browser.is_cancelled(session_id):
            await self._browser.send_response_complete(session_id, request_id)

    async def _end_session(self, session_id: str, session: SessionState) -> None:
        # Snapshot session data BEFORE closing — ws.py will remove the session
        # from memory on disconnect, so we need copies for the async pipeline.
        transcript_copy = list(session.transcript)
        discovery_copy = dict(session.discovery_sections)
        company_name = session.company_name or "Unknown"
        user_email = self._user_emails.pop(session_id, "")

        # Wait briefly for the last audio playback to finish before closing
        await self._browser.wait_for_playback(session_id, timeout=10.0)

        await self._browser.send_session_end(session_id)

        # Fire post-session pipeline with the snapshots
        if user_email:
            asyncio.create_task(
                run_post_session_pipeline(
                    session_id=session_id,
                    transcript=transcript_copy,
                    discovery_sections=discovery_copy,
                    company_name=company_name,
                    user_email=user_email,
                )
            )
            logger.info(
                "post_session_pipeline_fired",
                session_id=session_id,
                user_email=user_email,
            )
        else:
            logger.warning("post_session_no_email", session_id=session_id)

        await super()._end_session(session_id, session)
