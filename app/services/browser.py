"""Browser WebSocket connection manager for audio streaming.

Manages WebSocket connections between the browser client and the tedi-public
server. Handles message routing, audio streaming, and request cancellation
for barge-in support.
"""

import asyncio
import base64
from typing import Any
from uuid import uuid4

import structlog
from fastapi import WebSocket

logger = structlog.get_logger(__name__)


class BrowserService:
    """Manages WebSocket connections from browser clients.

    Each session opens a WebSocket to this server. This service tracks those
    connections and provides methods to stream audio and control playback.
    """

    def __init__(self) -> None:
        # session_id -> WebSocket connection
        self._connections: dict[str, WebSocket] = {}
        # session_id -> asyncio.Event signaling cancellation
        self._cancel_events: dict[str, asyncio.Event] = {}
        # session_id -> asyncio.Event signaling playback finished
        self._playback_done_events: dict[str, asyncio.Event] = {}

    async def register(self, session_id: str, websocket: WebSocket) -> None:
        """Register a browser WebSocket connection for a session."""
        old = self._connections.get(session_id)
        if old is not None:
            logger.warning("browser_replacing_connection", session_id=session_id)
            try:
                await old.close(code=1000, reason="replaced")
            except Exception:
                pass

        self._connections[session_id] = websocket
        self._cancel_events[session_id] = asyncio.Event()
        self._playback_done_events[session_id] = asyncio.Event()
        self._playback_done_events[session_id].set()  # Not playing initially
        logger.info("browser_connected", session_id=session_id)

    async def unregister(self, session_id: str) -> None:
        """Remove a browser connection when it disconnects."""
        self._connections.pop(session_id, None)
        self._cancel_events.pop(session_id, None)
        # Wake up anyone waiting for playback to finish
        done_event = self._playback_done_events.pop(session_id, None)
        if done_event:
            done_event.set()
        logger.info("browser_disconnected", session_id=session_id)

    def is_connected(self, session_id: str) -> bool:
        """Check if a browser is connected for a given session."""
        return session_id in self._connections

    def new_request_id(self) -> str:
        """Generate a unique request ID for tracking synthesis requests."""
        return str(uuid4())

    async def cancel_request(self, session_id: str) -> None:
        """Signal cancellation for any in-flight synthesis for this session."""
        event = self._cancel_events.get(session_id)
        if event:
            event.set()
            logger.info("browser_request_cancelled", session_id=session_id)

    def is_cancelled(self, session_id: str) -> bool:
        """Check if the current synthesis request has been cancelled."""
        event = self._cancel_events.get(session_id)
        return event.is_set() if event else False

    def reset_cancellation(self, session_id: str) -> None:
        """Reset the cancellation flag for a new synthesis request."""
        event = self._cancel_events.get(session_id)
        if event:
            event.clear()

    def mark_playback_started(self, session_id: str) -> None:
        """Mark that audio playback is in progress for a session."""
        event = self._playback_done_events.get(session_id)
        if event:
            event.clear()

    def mark_playback_finished(self, session_id: str) -> None:
        """Mark that audio playback has finished for a session."""
        event = self._playback_done_events.get(session_id)
        if event:
            event.set()

    async def wait_for_playback(self, session_id: str, timeout: float = 30.0) -> bool:
        """Wait until the browser signals playback is finished.

        Returns True if playback finished, False on timeout.
        """
        event = self._playback_done_events.get(session_id)
        if event is None:
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "browser_playback_wait_timeout",
                session_id=session_id,
                timeout=timeout,
            )
            return False

    async def close_connection(
        self, session_id: str, code: int = 1000, reason: str = ""
    ) -> None:
        """Close the WebSocket connection for a session."""
        ws = self._connections.get(session_id)
        if ws is not None:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass

    async def send_message(self, session_id: str, message: dict[str, Any]) -> bool:
        """Send a JSON message to the browser.

        Returns True if sent successfully, False if not connected.
        """
        ws = self._connections.get(session_id)
        if ws is None:
            logger.warning("browser_send_no_connection", session_id=session_id)
            return False

        try:
            await ws.send_json(message)
            return True
        except Exception:
            logger.exception("browser_send_failed", session_id=session_id)
            await self.unregister(session_id)
            return False

    async def send_audio_chunk(
        self,
        session_id: str,
        request_id: str,
        audio_chunk: bytes,
        is_final: bool = False,
    ) -> bool:
        """Send an audio chunk to the browser for playback."""
        if self.is_cancelled(session_id):
            return False

        b64_audio = base64.b64encode(audio_chunk).decode("ascii")
        return await self.send_message(session_id, {
            "type": "audio_chunk",
            "request_id": request_id,
            "audio_base64": b64_audio,
            "is_final": is_final,
        })

    async def send_thinking_start(self, session_id: str) -> bool:
        """Notify the browser that the server is processing."""
        return await self.send_message(session_id, {"type": "thinking_start"})

    async def send_response_start(
        self,
        session_id: str,
        request_id: str,
        spoken_text: str,
    ) -> bool:
        """Notify the browser that a new response is starting."""
        self.mark_playback_started(session_id)
        return await self.send_message(session_id, {
            "type": "response_start",
            "request_id": request_id,
            "spoken_text": spoken_text,
        })

    async def send_response_complete(self, session_id: str, request_id: str) -> bool:
        """Notify the browser that the response audio is complete."""
        return await self.send_message(session_id, {
            "type": "response_complete",
            "request_id": request_id,
        })

    async def send_stop_playback(self, session_id: str) -> bool:
        """Tell the browser to immediately stop playing any audio."""
        return await self.send_message(session_id, {"type": "stop_playback"})

    async def send_session_end(self, session_id: str) -> None:
        """Signal session over, then close the WebSocket connection."""
        await self.send_message(session_id, {"type": "session_end"})
        await self.close_connection(session_id, code=1000, reason="session_end")
