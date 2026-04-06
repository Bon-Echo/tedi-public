"""Runtime in-memory session state, per WebSocket connection.

Forked from Bon-Echo/tedi/app/session.py — stripped Recall.ai-specific
fields (bot_id, output_media_started, meeting_url, client_name,
project_name, output_mode).
"""

import asyncio
import uuid
from enum import Enum
from typing import Any


class TurnState(str, Enum):
    IDLE = "idle"
    TURN_RECEIVED = "turn_received"
    PROCESSING = "processing"
    SPEAKING = "speaking"


class RuntimeSessionState:
    """In-memory state for an active browser WebSocket session.

    Created when a WebSocket connects and destroyed on disconnect.
    Tracks conversation state, barge-in coordination, and turn management.
    """

    def __init__(self, session_id: uuid.UUID) -> None:
        self.session_id: uuid.UUID = session_id
        self.turn_state: TurnState = TurnState.IDLE
        self.active_request_id: str | None = None
        self.conversation_history: list[dict[str, str]] = []
        self.transcript: list[dict[str, Any]] = []
        self.tdd_sections: dict[str, Any] = {}
        self.lock: asyncio.Lock = asyncio.Lock()
        # Timeout task handle — set by the orchestrator when session activates
        self.timeout_task: asyncio.Task[None] | None = None
