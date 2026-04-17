"""Tests for the info-request email helper and pipeline gating."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import notifications as notif


@pytest.mark.asyncio
async def test_skips_when_no_documents():
    sent = AsyncMock()
    with patch.object(notif, "_send_raw_email", sent):
        await notif.send_info_request_email(
            user_email="founder@example.com",
            project_name="Acme",
            requested_documents=[],
        )
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_sends_with_bullets_and_subject():
    captured = {}

    async def fake_send(source, destinations, msg):
        captured["source"] = source
        captured["destinations"] = destinations
        captured["msg"] = msg.as_string()

    with patch.object(notif, "_send_raw_email", side_effect=fake_send):
        await notif.send_info_request_email(
            user_email="founder@example.com",
            project_name="Acme",
            requested_documents=["sample invoice PDF", "CRM API key"],
        )

    assert "founder@example.com" in captured["destinations"]
    body = captured["msg"]
    assert "Acme" in body
    assert "sample invoice PDF" in body
    assert "CRM API key" in body
    # Subject line is "A few things we'll need from you — Acme"
    assert "A few things we'll need from you" in body


@pytest.mark.asyncio
async def test_ccs_internal_recipients(monkeypatch):
    monkeypatch.setattr(
        notif.settings,
        "OUTPUT_RECIPIENTS",
        "labeeb@bonecho.ai,deep@bonecho.ai",
    )
    captured = {}

    async def fake_send(source, destinations, msg):
        captured["destinations"] = destinations

    with patch.object(notif, "_send_raw_email", side_effect=fake_send):
        await notif.send_info_request_email(
            user_email="founder@example.com",
            project_name="Acme",
            requested_documents=["sample invoice"],
        )

    assert "labeeb@bonecho.ai" in captured["destinations"]
    assert "deep@bonecho.ai" in captured["destinations"]
    # User must still be the primary recipient.
    assert captured["destinations"][0] == "founder@example.com"


# ---------------------------------------------------------------------------
# Slack notification accuracy — returned bool must reflect real delivery.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_returns_false_when_not_configured(monkeypatch):
    monkeypatch.setattr(notif.settings, "SLACK_WEBHOOK_URL", "")
    ok = await notif.notify_session_complete(
        user_email="founder@example.com",
        business_summary="summary",
        session_id="s-1",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_slack_returns_true_on_2xx(monkeypatch):
    monkeypatch.setattr(
        notif.settings, "SLACK_WEBHOOK_URL", "https://hooks.slack.example/xxx"
    )

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002 — matches httpx
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

    with patch.object(notif.httpx, "AsyncClient", return_value=_FakeClient()):
        ok = await notif.notify_session_complete(
            user_email="founder@example.com",
            business_summary="summary",
            session_id="s-2",
        )
    assert ok is True


@pytest.mark.asyncio
async def test_slack_returns_false_on_http_error(monkeypatch):
    monkeypatch.setattr(
        notif.settings, "SLACK_WEBHOOK_URL", "https://hooks.slack.example/xxx"
    )

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            resp = MagicMock()
            resp.raise_for_status.side_effect = httpx.HTTPError("boom")
            return resp

    with patch.object(notif.httpx, "AsyncClient", return_value=_FakeClient()):
        ok = await notif.notify_session_complete(
            user_email="founder@example.com",
            business_summary="summary",
            session_id="s-3",
        )
    assert ok is False
