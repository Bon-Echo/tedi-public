"""Tests for POST /api/v1/signup and GET /api/v1/session/{id}."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


class TestSignupEndpoint:
    def test_signup_invalid_email_returns_422(self, client):
        resp = client.post("/api/v1/signup", json={"email": "not-an-email"})
        assert resp.status_code == 422


class TestSignupService:
    @pytest.mark.asyncio
    async def test_daily_cap_enforced(self):
        from app.services.signup_service import SignupService
        from app.config import settings

        mock_db = AsyncMock()

        svc = SignupService(mock_db)

        with patch.object(svc, "get_daily_session_count", return_value=settings.DAILY_SESSION_CAP), \
             patch.object(svc, "get_waitlist_position", return_value=1):
            outcome, session, position = await svc.signup("test@example.com")

        assert outcome == "waitlisted"
        assert session is None
        assert position == 1

    @pytest.mark.asyncio
    async def test_signup_creates_user_and_session(self):
        from app.services.signup_service import SignupService

        mock_db = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = uuid.uuid4()
        mock_session = MagicMock()
        mock_session.id = uuid.uuid4()
        mock_session.token = uuid.uuid4()

        svc = SignupService(mock_db)

        with patch.object(svc, "get_daily_session_count", return_value=0), \
             patch.object(svc, "upsert_user", return_value=mock_user), \
             patch.object(svc, "create_session", return_value=mock_session):
            outcome, session, position = await svc.signup("test@example.com")

        assert outcome == "created"
        assert session == mock_session
        assert position is None
