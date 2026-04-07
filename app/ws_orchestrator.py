"""WebSocketOrchestrator — bridges Orchestrator to BrowserService for browser sessions."""

import structlog

from app.orchestrator import Orchestrator
from app.services.browser import BrowserService
from app.services.claude import ClaudeService
from app.services.elevenlabs import ElevenLabsService
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

    async def on_speech_final(self, session_id: str, text: str) -> None:
        """Send thinking_start before delegating to the base turn pipeline."""
        await self._browser.send_thinking_start(session_id)
        await super().on_speech_final(session_id, text)

    def _is_cancelled(self, session_id: str) -> bool:
        return self._browser.is_cancelled(session_id)

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
        await self._browser.send_session_end(session_id)
        await super()._end_session(session_id, session)
