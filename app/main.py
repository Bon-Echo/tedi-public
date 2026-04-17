import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

import anthropic
import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import settings
from app.database import init_db, close_db
from app.middleware.rate_limit import limiter
from app.routers import admin as admin_router
from app.routers import admin_api, auth, health
from app.routers import signup, session_router
from app.routers import ws as ws_router
from app.schemas import ErrorResponse
from app.services.browser import BrowserService
from app.services.claude import ClaudeService
from app.services.elevenlabs import ElevenLabsService
from app.session import SessionManager
from app.ws_orchestrator import WebSocketOrchestrator

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
    session_manager = SessionManager()

    orchestrator = WebSocketOrchestrator(
        browser_service=browser_service,
        session_manager=session_manager,
        claude_service=claude_service,
        elevenlabs_service=elevenlabs_service,
        post_session_service=None,
    )

    app.state.browser_service = browser_service
    app.state.orchestrator = orchestrator
    app.state.session_manager = session_manager

    # Validate Anthropic API key at startup to fail fast on misconfiguration
    try:
        await claude_service._client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        logger.info("claude_api_key_valid")
    except anthropic.AuthenticationError:
        logger.critical(
            "claude_api_key_invalid",
            hint="Check ANTHROPIC_API_KEY in .env or GitHub Secrets",
        )
        raise SystemExit("FATAL: Invalid ANTHROPIC_API_KEY — cannot start")

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

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Public Tedi origins (signup, room, ws). Admin/auth surface is protected
    # by the per-route middleware below, which restricts cookied requests to
    # the admin UI origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list + settings.admin_cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def admin_origin_guard(request: Request, call_next: object) -> Response:
        """Block cross-origin requests to /api/admin and /auth from non-admin
        origins.

        CORS already prevents browsers from reading responses, but we also
        reject the request server-side so the public Tedi origin cannot drive
        admin actions even if a cookie is present.
        """
        path = request.url.path
        if path.startswith("/api/admin") or path.startswith("/auth"):
            origin = request.headers.get("origin")
            if origin and origin not in settings.admin_cors_origins_list:
                return JSONResponse(
                    status_code=403,
                    content=ErrorResponse(
                        error="forbidden_origin",
                        message="origin not permitted for admin surface",
                    ).model_dump(),
                )
        return await call_next(request)  # type: ignore[misc]

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
    app.include_router(session_router.router)
    app.include_router(ws_router.router)
    app.include_router(admin_router.router)
    app.include_router(auth.router)
    app.include_router(admin_api.router)

    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

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
