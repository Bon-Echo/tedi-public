"""Production must refuse to boot with the placeholder admin session secret."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, _ADMIN_SESSION_SECRET_DEV_DEFAULT


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "ANTHROPIC_API_KEY": "test-key-pretending-to-be-long-enough-for-validator",
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
    }
    env.update(overrides)
    return env


def test_dev_accepts_placeholder_admin_secret(monkeypatch):
    for k, v in _base_env(APP_ENV="development").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ADMIN_SESSION_SECRET", raising=False)
    s = Settings()
    assert s.ADMIN_SESSION_SECRET == _ADMIN_SESSION_SECRET_DEV_DEFAULT


def test_production_rejects_placeholder_admin_secret(monkeypatch):
    env = _base_env(
        APP_ENV="production",
        ADMIN_SESSION_SECRET=_ADMIN_SESSION_SECRET_DEV_DEFAULT,
    )
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "ADMIN_SESSION_SECRET" in str(exc.value)


def test_production_rejects_short_admin_secret(monkeypatch):
    for k, v in _base_env(
        APP_ENV="production", ADMIN_SESSION_SECRET="too-short"
    ).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        Settings()


def test_production_accepts_strong_admin_secret(monkeypatch):
    strong = "a" * 64
    for k, v in _base_env(
        APP_ENV="production", ADMIN_SESSION_SECRET=strong
    ).items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.ADMIN_SESSION_SECRET == strong
