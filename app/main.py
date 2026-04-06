"""FastAPI application factory for tedi-public."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Tedi Public",
        description="Public-facing Tedi — self-serve discovery sessions",
        version="0.1.0",
    )

    # Static assets
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Health check — required by CI/CD and load balancer
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # Routers added by Backend Engineer (WS2)
    # from app.routers import signup, session, websocket
    # app.include_router(signup.router)
    # app.include_router(session.router)
    # app.include_router(websocket.router)

    return app
