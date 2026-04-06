from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str
    message: str


class HealthResponse(BaseModel):
    status: str
    version: str = "1.0.0"
