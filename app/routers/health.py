from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> JSONResponse:
    return JSONResponse(content={"status": "ok", "version": "1.0.0"})
