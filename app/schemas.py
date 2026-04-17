from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class DiscoveryArea(str, Enum):
    BUSINESS_CONTEXT = "business_context"
    PAIN_POINTS = "pain_points"
    AGENT_OPPORTUNITIES = "agent_opportunities"


class SessionPhase(str, Enum):
    OPENING = "opening"
    DISCOVERY = "discovery"
    WRAPPING_UP = "wrapping_up"
    CLOSING = "closing"


class DiscoveryUpdate(BaseModel):
    area: DiscoveryArea = Field(..., description="Discovery area to update")
    content: str = Field(..., description="Information extracted from this turn")
    action: Literal["append", "replace"] = Field(
        ..., description="How to apply the update"
    )


class Coverage(BaseModel):
    business_context: int = Field(default=0, ge=0, le=100)
    pain_points: int = Field(default=0, ge=0, le=100)
    agent_opportunities: int = Field(default=0, ge=0, le=100)

    def to_dict(self) -> dict[str, int]:
        return {
            "business_context": self.business_context,
            "pain_points": self.pain_points,
            "agent_opportunities": self.agent_opportunities,
        }

    def average(self) -> float:
        values = [
            self.business_context,
            self.pain_points,
            self.agent_opportunities,
        ]
        return sum(values) / len(values)


class DiscoveryResponse(BaseModel):
    spoken_response: str = Field(..., description="Text to speak back to the user")
    discovery_updates: list[DiscoveryUpdate] = Field(
        default_factory=list, description="Discovery section updates from this turn"
    )
    coverage: Coverage = Field(
        default_factory=Coverage, description="Coverage percentages per area"
    )
    internal_notes: str | None = Field(
        None, description="Internal notes not spoken aloud"
    )
    session_phase: SessionPhase = Field(
        default=SessionPhase.OPENING, description="Current session phase"
    )
    elapsed_minutes: float = Field(
        default=0.0, description="Elapsed session time in minutes"
    )


# --- API request/response models ---


class CreateSessionRequest(BaseModel):
    client_name: str | None = Field(None, description="Name of the client")
    company_name: str | None = Field(None, description="Name of the company")


class SessionResponse(BaseModel):
    session_id: str = Field(..., description="Unique session identifier (UUID)")
    status: str
    client_name: str | None = None
    company_name: str | None = None
    created_at: datetime
    session_phase: str
    elapsed_minutes: float
    coverage: dict[str, int]


class SpeechInput(BaseModel):
    text: str = Field(..., description="Transcribed user speech")
    session_id: str = Field(..., description="Session identifier")


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: list[dict[str, Any]] | None = None


# --- Signup ---


class SignupRequest(BaseModel):
    email: EmailStr = Field(..., description="User email address")


class SignupCreatedResponse(BaseModel):
    sessionToken: str = Field(..., description="Session token UUID")
    roomUrl: str = Field(..., description="URL to the browser room")


class SignupWaitlistedResponse(BaseModel):
    message: str = Field(..., description="Waitlist message")
    position: int = Field(..., description="Approximate queue position")


# --- Admin dashboard ---


class ManualFollowupRequest(BaseModel):
    body: str = Field(..., min_length=1, description="Plain-text email body")
    subject: str | None = Field(
        default=None, description="Optional subject; server fills a default if omitted"
    )


class ManualFollowupResponse(BaseModel):
    ok: bool
    sentAt: datetime
    auditId: str
