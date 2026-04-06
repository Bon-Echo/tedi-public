import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import settings
from app.database import init_db, close_db
from app.middleware.rate_limit import limiter
from app.routers import health, signup, session, browser_ws
from app.schemas import ErrorResponse
from app.services.browser import BrowserService
from app.services.claude import ClaudeService
from app.services.elevenlabs import ElevenLabsService
from app.services.orchestrator import Orchestrator
from app.services.session_service import SessionService

logger = structlog.get_logger(__name__)


def configure_logging() -> None:
    import logging

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info(
        "application_starting",
        app_name=settings.APP_NAME,
        env=settings.APP_ENV,
    )

    await init_db()

    browser_service = BrowserService()
    claude_service = ClaudeService()
    elevenlabs_service = ElevenLabsService()
    session_service = SessionService()
    orchestrator = Orchestrator(
        browser_service=browser_service,
        claude_service=claude_service,
        elevenlabs_service=elevenlabs_service,
        session_service=session_service,
    )

    app.state.browser_service = browser_service
    app.state.orchestrator = orchestrator

    yield

    await close_db()
    logger.info("application_shutting_down", app_name=settings.APP_NAME)


def create_app() -> FastAPI:
    is_production = settings.APP_ENV == "production"

    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # Rate limiter state
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_logging_middleware(
        request: Request, call_next: object
    ) -> Response:
        request_id = str(uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        start_time = time.monotonic()
        logger.info("request_started")

        try:
            response: Response = await call_next(request)  # type: ignore[misc]
        except Exception:
            logger.exception("request_failed")
            return JSONResponse(
                status_code=500,
                content=ErrorResponse(
                    error="internal_server_error",
                    message="An unexpected error occurred",
                ).model_dump(),
            )

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        logger.info(
            "request_completed",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(health.router)
    app.include_router(signup.router)
    app.include_router(session.router)
    app.include_router(browser_ws.router)

    return app


if __name__ == "__main__":
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.HOST,
        port=settings.PORT,
        workers=1,
        reload=settings.APP_ENV == "development",
    )
