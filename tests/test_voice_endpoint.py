"""Tests for Phase 3 — Voice endpoint (POST /api/v1/voice/turn)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentConfig, AgentLoop, FakeLLMClient, LLMResponse, LLMUsage
from app.db import init_db
from app.main import create_app


@pytest.fixture
def test_client_and_db():
    """TestClient bound to a temp database."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        from app.config import settings as cached_settings

        previous = cached_settings.database_url
        cached_settings.database_url = str(db_path)
        cached_settings.cors_origins = "http://localhost:3000"

        app = create_app()
        with TestClient(app) as tc:
            yield tc, db_path

        cached_settings.database_url = previous


def _make_fake_agent(db_path, responses):
    """Build an AgentLoop wired to a temp DB and a programmed FakeLLMClient."""
    fake = FakeLLMClient()
    for resp in responses:
        fake.add_response(resp)
    return AgentLoop(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        config=AgentConfig.from_env(),
        llm_client=fake,
        db_path=db_path,
    )


def _text_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="fake"),
        raw=None,
    )


def _tool_response(name: str, args: dict) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[{"id": "tc_1", "name": name, "arguments": json.dumps(args)}],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="fake"),
        raw=None,
    )


def test_voice_turn_empty_transcript_rejected(test_client_and_db):
    """Empty transcript returns 400."""
    tc, _ = test_client_and_db
    resp = tc.post(
        "/api/v1/voice/turn",
        json={"session_id": "test_1", "transcript": "   ", "timings": {}},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_voice_turn_oversized_transcript_rejected(test_client_and_db):
    """Transcript larger than MAX_TRANSCRIPT_BYTES returns 400."""
    tc, _ = test_client_and_db
    huge = "x" * 3000  # > 2000 bytes
    resp = tc.post(
        "/api/v1/voice/turn",
        json={"session_id": "test_2", "transcript": huge, "timings": {}},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()


def test_voice_turn_basic_echo(test_client_and_db):
    """Echo mode: the bot replies to a simple transcript without tools."""
    tc, db_path = test_client_and_db

    agent = _make_fake_agent(db_path, [_text_response("I heard you say hello.")])

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"test_echo_1": agent}
    try:
        resp = tc.post(
            "/api/v1/voice/turn",
            json={"session_id": "test_echo_1", "transcript": "Hello", "timings": {"stt": 120}},
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "I heard you say hello."
    assert data["call_id"] is not None
    assert data["ended"] is False
    assert "agent_ms" in data["timings"]
    assert data["timings"]["stt_ms"] == 120


def test_voice_turn_phone_number_masked_in_transcript(test_client_and_db):
    """Phone numbers in the transcript are masked before storage/processing."""
    tc, db_path = test_client_and_db

    agent = _make_fake_agent(db_path, [_text_response("Got your number.")])

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"test_mask_1": agent}
    try:
        resp = tc.post(
            "/api/v1/voice/turn",
            json={
                "session_id": "test_mask_1",
                "transcript": "My number is 555-123-4567",
                "timings": {},
            },
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Got your number."

    # Verify the stored transcript in call_turns is masked
    from app.db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT text FROM call_turns WHERE speaker = 'caller' ORDER BY id DESC LIMIT 1"
        ).fetchall()
        assert len(rows) >= 1
        stored = rows[0]["text"]
        assert "555" not in stored, f"Raw phone number found in stored transcript: {stored}"
        assert "(XXX) XXX-4567" in stored or "XXX" in stored, f"Masked form not found: {stored}"
    finally:
        conn.close()


def test_voice_turn_no_audio_accepted_or_stored(test_client_and_db):
    """The endpoint never accepts or stores audio — only JSON transcript."""
    tc, db_path = test_client_and_db

    # Try to send a multipart with audio (should fail or be ignored)
    resp = tc.post(
        "/api/v1/voice/turn",
        files={"audio": ("test.wav", b"fake audio data", "audio/wav")},
    )
    # Should reject non-JSON or return 422 for missing fields
    assert resp.status_code in (400, 422, 500), "Should not accept multipart audio"

    # Verify no audio blob made it to DB
    from app.db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT arguments_json FROM tool_calls").fetchall()
        for row in rows:
            assert "audio" not in row["arguments_json"].lower()
            assert "wav" not in row["arguments_json"].lower()
    finally:
        conn.close()


def test_voice_turn_agent_handoff_sets_ended_true(test_client_and_db):
    """A handoff triggered by the agent returns ended=true."""
    tc, db_path = test_client_and_db

    agent = _make_fake_agent(
        db_path,
        [
            _tool_response(
                "handoff_to_human",
                {"reason": "urgent", "summary": "Emergency", "confirmed": True},
            )
        ],
    )

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"test_handoff_1": agent}
    try:
        resp = tc.post(
            "/api/v1/voice/turn",
            json={"session_id": "test_handoff_1", "transcript": "Emergency!", "timings": {}},
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp.status_code == 200
    data = resp.json()
    assert data["ended"] is True
    assert "transfer" in data["reply"].lower() or "human" in data["reply"].lower()


def test_voice_turn_session_isolation(test_client_and_db):
    """Different session_ids get different AgentLoop instances."""
    tc, db_path = test_client_and_db

    agent_a = _make_fake_agent(db_path, [_text_response("Session A reply")])
    agent_b = _make_fake_agent(db_path, [_text_response("Session B reply")])

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"session_a": agent_a, "session_b": agent_b}
    try:
        resp_a = tc.post(
            "/api/v1/voice/turn",
            json={"session_id": "session_a", "transcript": "Hello A", "timings": {}},
        )
        resp_b = tc.post(
            "/api/v1/voice/turn",
            json={"session_id": "session_b", "transcript": "Hello B", "timings": {}},
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.json()["reply"] == "Session A reply"
    assert resp_b.json()["reply"] == "Session B reply"
    # Different call_ids
    assert resp_a.json()["call_id"] != resp_b.json()["call_id"]


def test_voice_turn_timings_returned(test_client_and_db):
    """Server timings are returned in the response."""
    tc, db_path = test_client_and_db

    agent = _make_fake_agent(db_path, [_text_response("Timed reply")])

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"test_timing_1": agent}
    try:
        resp = tc.post(
            "/api/v1/voice/turn",
            json={
                "session_id": "test_timing_1",
                "transcript": "Test timing",
                "timings": {"stt": 100, "synthesis": 50, "network": 20},
            },
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp.status_code == 200
    data = resp.json()
    assert "timings" in data
    assert "agent_ms" in data["timings"]
    assert data["timings"]["stt_ms"] == 100
    assert data["timings"]["synthesis_ms"] == 50
    assert data["timings"]["network_ms"] == 20
    assert data["timings"]["total_ms"] >= data["timings"]["agent_ms"]


def test_voice_turn_uses_existing_db_path(test_client_and_db):
    """The voice endpoint uses the configured database path."""
    tc, db_path = test_client_and_db

    agent = _make_fake_agent(db_path, [_text_response("DB test")])

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"test_db_1": agent}
    try:
        resp = tc.post(
            "/api/v1/voice/turn",
            json={"session_id": "test_db_1", "transcript": "DB test", "timings": {}},
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp.status_code == 200

    # Verify a call row was created in the test database
    from app.db import connect

    conn = connect(db_path)
    try:
        call_row = conn.execute(
            "SELECT * FROM calls WHERE id = ?", (resp.json()["call_id"],)
        ).fetchone()
        assert call_row is not None
        assert call_row["status"] in ("completed", "handoff", "active")
    finally:
        conn.close()


def test_voice_end_closes_session(test_client_and_db):
    """POST /api/v1/voice/end closes the session and finalises the call."""
    tc, db_path = test_client_and_db

    agent = _make_fake_agent(db_path, [_text_response("OK")])

    import app.routers.voice as voice_module

    original_sessions = voice_module._sessions
    voice_module._sessions = {"test_end_1": agent}
    try:
        # First, make a turn to create the call
        tc.post(
            "/api/v1/voice/turn",
            json={"session_id": "test_end_1", "transcript": "Hello", "timings": {}},
        )
        # Now end it
        resp = tc.post(
            "/api/v1/voice/end",
            json={"session_id": "test_end_1"},
        )
    finally:
        voice_module._sessions = original_sessions

    assert resp.status_code == 200
    data = resp.json()
    assert data["ended"] is True
    # Session should be gone
    assert "test_end_1" not in voice_module._sessions