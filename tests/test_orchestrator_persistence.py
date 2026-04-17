"""Verify the orchestrator schedules turn persistence for user + agent turns.

We patch `schedule_persist_turn` at the module level so we can assert the
sequence of (seq, speaker, text) calls without a real database.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import orchestrator as orch_mod
from app.schemas import Coverage, DiscoveryResponse, SessionPhase
from app.session import SessionManager


@pytest.mark.asyncio
async def test_orchestrator_schedules_user_and_agent_turns(monkeypatch):
    calls: list[tuple] = []

    def capture(session_id, seq, speaker, text):
        calls.append((session_id, seq, speaker, text))

    monkeypatch.setattr(orch_mod, "schedule_persist_turn", capture)

    sm = SessionManager()
    session = sm.create_session()

    fake_claude = MagicMock()
    fake_claude.generate_response = AsyncMock(
        return_value=DiscoveryResponse(
            spoken_response="Hi there, tell me about your business.",
            discovery_updates=[],
            coverage=Coverage(),
            session_phase=SessionPhase.OPENING,
            elapsed_minutes=0.1,
        )
    )
    fake_eleven = MagicMock()

    async def fake_tts(*args, **kwargs):
        if False:  # async generator with no chunks
            yield b""

    fake_eleven.text_to_speech_streamed = fake_tts

    o = orch_mod.Orchestrator(
        session_manager=sm,
        claude_service=fake_claude,
        elevenlabs_service=fake_eleven,
    )

    await o.on_speech_final(session.session_id, "We make widgets.")

    # Allow the awaited tasks (synthesis no-op) to settle.
    await asyncio.sleep(0)

    assert len(calls) == 2
    user_call, agent_call = calls
    assert user_call[0] == session.session_id
    assert user_call[1] == 0
    assert user_call[2] == "user"
    assert user_call[3] == "We make widgets."
    assert agent_call[1] == 1
    assert agent_call[2] == "agent"
    assert agent_call[3].startswith("Hi there")
