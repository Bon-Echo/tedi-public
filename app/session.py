import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)


class SessionStatus(str, Enum):
    INITIALIZING = "initializing"
    ACTIVE = "active"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    POST_SESSION = "post_session"
    COMPLETED = "completed"
    ERROR = "error"


class SessionPhase(str, Enum):
    OPENING = "opening"
    DISCOVERY = "discovery"
    WRAPPING_UP = "wrapping_up"
    CLOSING = "closing"


class TurnState(str, Enum):
    IDLE = "idle"
    TURN_RECEIVED = "turn_received"
    PROCESSING = "processing"
    SPEAKING = "speaking"


# Phase transition thresholds (minutes)
PHASE_DISCOVERY_START = 2.0
PHASE_WRAPPING_UP_START = 15.0
PHASE_CLOSING_START = 18.0

# Conversation history window (number of messages to send to Claude)
CONVERSATION_HISTORY_WINDOW = 40


class SessionState:
    def __init__(
        self,
        client_name: str | None = None,
        company_name: str | None = None,
    ) -> None:
        self.session_id: str = str(uuid4())
        self.client_name: str | None = client_name
        self.company_name: str | None = company_name
        self.status: SessionStatus = SessionStatus.INITIALIZING
        self.turn_state: TurnState = TurnState.IDLE
        self.session_phase: SessionPhase = SessionPhase.OPENING

        # Transcript and conversation tracking
        self.transcript: list[dict[str, Any]] = []
        self.conversation_history: list[dict[str, str]] = []

        # Discovery-specific state (replaces tdd_sections)
        self.discovery_sections: dict[str, str] = {
            "business_overview": "",
            "dispatch_capacity": "",
            "hiring_seasonality": "",
            "fleet_equipment": "",
            "knowledge_transfer": "",
        }
        self.coverage: dict[str, int] = {
            "business_overview": 0,
            "dispatch_capacity": 0,
            "hiring_seasonality": 0,
            "fleet_equipment": 0,
            "knowledge_transfer": 0,
        }

        # Session timing
        self.session_start_time: datetime = datetime.now(timezone.utc)
        self.created_at: datetime = self.session_start_time

        # Barge-in / playback support
        self.lock: asyncio.Lock = asyncio.Lock()
        self.active_request_id: str | None = None

    def elapsed_minutes(self) -> float:
        """Return elapsed session time in minutes."""
        delta = datetime.now(timezone.utc) - self.session_start_time
        return delta.total_seconds() / 60.0

    def compute_phase(self) -> SessionPhase:
        """Derive session phase from elapsed time."""
        elapsed = self.elapsed_minutes()
        if elapsed >= PHASE_CLOSING_START:
            return SessionPhase.CLOSING
        if elapsed >= PHASE_WRAPPING_UP_START:
            return SessionPhase.WRAPPING_UP
        if elapsed >= PHASE_DISCOVERY_START:
            return SessionPhase.DISCOVERY
        return SessionPhase.OPENING

    def update_phase(self) -> bool:
        """Recompute phase and update state. Returns True if phase changed."""
        new_phase = self.compute_phase()
        if new_phase != self.session_phase:
            old_phase = self.session_phase
            self.session_phase = new_phase
            logger.info(
                "session_phase_transition",
                session_id=self.session_id,
                from_phase=old_phase.value,
                to_phase=new_phase.value,
                elapsed_minutes=round(self.elapsed_minutes(), 1),
            )
            return True
        return False

    def to_summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "client_name": self.client_name,
            "company_name": self.company_name,
            "status": self.status.value,
            "session_phase": self.session_phase.value,
            "elapsed_minutes": round(self.elapsed_minutes(), 1),
            "coverage": self.coverage,
            "created_at": self.created_at.isoformat(),
            "transcript_length": len(self.transcript),
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def create_session(
        self,
        client_name: str | None = None,
        company_name: str | None = None,
    ) -> SessionState:
        session = SessionState(
            client_name=client_name,
            company_name=company_name,
        )
        self._sessions[session.session_id] = session
        logger.info(
            "session_created",
            session_id=session.session_id,
            client_name=client_name,
            company_name=company_name,
        )
        return session

    def get_session(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        removed = self._sessions.pop(session_id, None)
        if removed:
            logger.info("session_removed", session_id=session_id)
        else:
            logger.warning("session_not_found_for_removal", session_id=session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [session.to_summary() for session in self._sessions.values()]
