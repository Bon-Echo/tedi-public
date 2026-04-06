import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr


class ErrorResponse(BaseModel):
    error: str
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str = "1.0.0"


# --- Signup ---

class SignupRequest(BaseModel):
    email: EmailStr


class SignupCreatedResponse(BaseModel):
    status: str = "created"
    sessionToken: str
    roomUrl: str


class SignupWaitlistedResponse(BaseModel):
    status: str = "waitlisted"
    message: str
    position: int


# --- Session ---

class SessionResponse(BaseModel):
    id: uuid.UUID
    status: str
    startedAt: datetime | None = None
    timeoutAt: datetime | None = None


# --- Claude / TDD ---

class TDDUpdate(BaseModel):
    section: str
    content: str
    action: str  # "append" or "replace"


class ClaudeResponse(BaseModel):
    spoken_response: str
    tdd_updates: list[TDDUpdate] = []
    internal_notes: str = ""
    should_leave: bool = False
