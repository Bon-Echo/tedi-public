from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class DiscoveryArea(str, Enum):
    BUSINESS_OVERVIEW = "business_overview"
    DISPATCH_CAPACITY = "dispatch_capacity"
    HIRING_SEASONALITY = "hiring_seasonality"
    FLEET_EQUIPMENT = "fleet_equipment"
    KNOWLEDGE_TRANSFER = "knowledge_transfer"


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
    business_overview: int = Field(default=0, ge=0, le=100)
    dispatch_capacity: int = Field(default=0, ge=0, le=100)
    hiring_seasonality: int = Field(default=0, ge=0, le=100)
    fleet_equipment: int = Field(default=0, ge=0, le=100)
    knowledge_transfer: int = Field(default=0, ge=0, le=100)

    def to_dict(self) -> dict[str, int]:
        return {
            "business_overview": self.business_overview,
            "dispatch_capacity": self.dispatch_capacity,
            "hiring_seasonality": self.hiring_seasonality,
            "fleet_equipment": self.fleet_equipment,
            "knowledge_transfer": self.knowledge_transfer,
        }

    def average(self) -> float:
        values = [
            self.business_overview,
            self.dispatch_capacity,
            self.hiring_seasonality,
            self.fleet_equipment,
            self.knowledge_transfer,
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
