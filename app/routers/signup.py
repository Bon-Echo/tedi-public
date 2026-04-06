import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.rate_limit import limiter, signup_rate_limit
from app.schemas import SignupCreatedResponse, SignupRequest, SignupWaitlistedResponse
from app.services.email_service import EmailService, send_confirmation_fire_and_forget
from app.services.signup_service import SignupService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["signup"])

_email_service = EmailService()


@router.post("/signup")
@limiter.limit(signup_rate_limit)
async def signup(
    request: Request,
    body: SignupRequest,
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    svc = SignupService(db)
    outcome, session, position = await svc.signup(body.email)

    if outcome == "waitlisted":
        return JSONResponse(
            status_code=200,
            content=SignupWaitlistedResponse(
                message="You're next! We'll email you when a slot opens.",
                position=position,
            ).model_dump(),
        )

    room_url = f"/room/{session.id}"
    send_confirmation_fire_and_forget(_email_service, body.email, room_url)

    return JSONResponse(
        status_code=201,
        content=SignupCreatedResponse(
            sessionToken=str(session.token),
            roomUrl=room_url,
        ).model_dump(),
    )
