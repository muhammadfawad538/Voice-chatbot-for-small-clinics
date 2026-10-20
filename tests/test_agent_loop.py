"""Tests for Phase 2b — Text Agent Loop."""

from __future__ import annotations

import json

import pytest

from app.agent import AgentConfig, AgentLoop, FakeLLMClient, LLMResponse, LLMUsage, run_conversation
from app.agent.schemas import get_tool_definitions, validate_tool_arguments
from app.db import connect, init_db
from app.tools.calendar import reset_calendar_client

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Fresh database for each test."""
    path = tmp_path / "test.db"
    init_db(path)

    from app.config import settings as cached_settings

    previous = cached_settings.database_url
    cached_settings.database_url = str(path)
    try:
        yield path
    finally:
        cached_settings.database_url = previous
        reset_calendar_client()


@pytest.fixture
def fake_llm():
    """A fake LLM client that can be programmed with responses."""
    return FakeLLMClient()


# ---------------------------------------------------------------------------
# Response builders — keep the test bodies about behaviour, not boilerplate
# ---------------------------------------------------------------------------


def text(content: str) -> LLMResponse:
    """A fake LLM response that only speaks."""
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="fake"),
        raw=None,
    )


def tool_call(name: str, args: dict | None = None, raw_args: str | None = None) -> LLMResponse:
    """A fake LLM response that calls one tool.

    ``raw_args`` is used to hand the model unparseable JSON, which is a case
    the tests need to reach directly.
    """
    return LLMResponse(
        content=None,
        tool_calls=[{
            "id": "tc_1",
            "name": name,
            "arguments": raw_args if raw_args is not None else json.dumps(args or {}),
        }],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="fake"),
        raw=None,
    )


def make_agent(db_path, llm, masked="(XXX) XXX-4567", config=None) -> AgentLoop:
    """An AgentLoop wired to a temp database and a programmed fake LLM."""
    return AgentLoop(
        caller_number="(555) 123-4567",
        caller_number_masked=masked,
        config=config,
        llm_client=llm,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Tool schema validation
# ---------------------------------------------------------------------------


def test_tool_definitions_generated():
    """All five tools have schemas derived from Pydantic models."""
    defs = get_tool_definitions()
    assert len(defs) == 5
    names = {d["function"]["name"] for d in defs}
    assert names == {
        "check_availability",
        "book_slot",
        "reschedule",
        "cancel",
        "handoff_to_human",
    }
    for d in defs:
        assert d["type"] == "function"
        assert "function" in d
        assert "name" in d["function"]
        assert "parameters" in d["function"]
        # Internal fields stripped
        props = d["function"]["parameters"].get("properties", {})
        assert "failure_mode" not in props
        assert "injected" not in props
        # confirmed is REQUIRED and explicit - the LLM must choose it
        assert "confirmed" in props
        req = d["function"]["parameters"].get("required", [])
        assert "confirmed" in req


def test_validate_tool_arguments_accepts_valid():
    """Valid arguments pass validation."""
    ok, err, model = validate_tool_arguments(
        "book_slot",
        {
            "caller_name": "Ada",
            "caller_number": "(555) 123-4567",
            "date": "2026-11-02",
            "time": "10:00",
            "confirmed": True,
        },
    )
    assert ok is True
    assert err is None
    assert model is not None
    assert model.caller_name == "Ada"


def test_validate_tool_arguments_rejects_missing_confirmed():
    """Write tools require confirmed=true."""
    ok, err, _ = validate_tool_arguments(
        "book_slot",
        {
            "caller_name": "Ada",
            "caller_number": "(555) 123-4567",
            "date": "2026-11-02",
            "time": "10:00",
            "confirmed": False,
        },
    )
    assert ok is False
    assert "confirmed" in err.lower()


def test_validate_tool_arguments_rejects_invalid_date():
    """Invalid date format is rejected."""
    ok, err, _ = validate_tool_arguments(
        "check_availability",
        {"date": "02-11-2026", "confirmed": True},
    )
    assert ok is False
    assert err is not None


# ---------------------------------------------------------------------------
# AgentLoop basic behaviour
# ---------------------------------------------------------------------------


def test_agent_loop_creates_call_row(db_path, fake_llm):
    """A call row is created on first receive."""
    fake_llm.add_response(text("Hello! How can I help?"))
    agent = make_agent(db_path, fake_llm)
    agent.receive("Hi")
    assert agent.state.call_id is not None
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (agent.state.call_id,)).fetchone()
        assert row is not None
        assert row["status"] == "active"
        assert row["caller_number_masked"] == "(XXX) XXX-4567"
    finally:
        conn.close()


def test_agent_loop_logs_turns(db_path, fake_llm):
    """Both caller and bot turns are logged."""
    fake_llm.add_response(text("Hello! How can I help?"))
    agent = make_agent(db_path, fake_llm)
    agent.receive("Hi")
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT speaker, text FROM call_turns WHERE call_id = ? ORDER BY id",
            (agent.state.call_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["speaker"] == "caller"
        assert rows[0]["text"] == "Hi"
        assert rows[1]["speaker"] == "bot"
        assert rows[1]["text"] == "Hello! How can I help?"
    finally:
        conn.close()


def test_agent_loop_end_call_updates_status(db_path, fake_llm):
    """end_call marks the call completed."""
    fake_llm.add_response(text("Goodbye!"))
    agent = make_agent(db_path, fake_llm)
    agent.receive("Hi")
    agent.end_call()
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, ended_at FROM calls WHERE id = ?",
            (agent.state.call_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["ended_at"] is not None
    finally:
        conn.close()


def test_agent_loop_handoff_updates_status(db_path, fake_llm):
    """Handoff marks the call as handoff."""
    fake_llm.add_response(
        tool_call(
            "handoff_to_human",
            {"reason": "urgent", "summary": "Caller says emergency", "confirmed": True},
        )
    )
    agent = make_agent(db_path, fake_llm)
    agent.receive("Emergency!")
    assert agent.state.handoff_triggered is True
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM calls WHERE id = ?",
            (agent.state.call_id,),
        ).fetchone()
        assert row["status"] == "handoff"
        handoff = conn.execute(
            "SELECT * FROM handoffs WHERE call_id = ?",
            (agent.state.call_id,),
        ).fetchone()
        assert handoff is not None
        assert handoff["reason"] == "urgent"
    finally:
        conn.close()


def test_agent_loop_step_limit_triggers_handoff(db_path, fake_llm):
    """Exceeding max_steps triggers handoff with reason='unsure'."""
    config = AgentConfig(max_steps=2)
    # LLM keeps asking for tools forever
    for _ in range(5):
        fake_llm.add_response(
            tool_call("check_availability", {"date": "2026-11-02", "confirmed": True})
        )
    agent = make_agent(db_path, fake_llm, config=config)
    reply = agent.receive("Hi")
    assert "transfer" in reply.lower() or "human" in reply.lower()
    assert agent.state.handoff_triggered is True


# ---------------------------------------------------------------------------
# Booking flow
# ---------------------------------------------------------------------------


def test_full_booking_flow(db_path):
    """End-to-end booking: check -> book with confirmation."""
    llm = FakeLLMClient()
    # Turn 1: caller wants to book -> LLM calls check_availability
    llm.add_response(tool_call("check_availability", {"date": "2026-11-02", "confirmed": True}))
    # Turn 2: LLM responds with slots -> caller picks one -> LLM reads back
    llm.add_response(
        text("I have 10:00 AM and 10:30 AM open on November 2nd. Which would you prefer?")
    )
    # Turn 3: caller says "10 AM" -> LLM reads back and asks for confirmation
    llm.add_response(
        text("Let me confirm: I'm booking Ada for November 2nd at 10:00 AM. Is that right?")
    )
    # Turn 4: caller says "yes" -> LLM calls book_slot with confirmed=true
    llm.add_response(
        tool_call(
            "book_slot",
            {
                "caller_name": "Ada",
                "caller_number": "(555) 123-4567",
                "date": "2026-11-02",
                "time": "10:00",
                "confirmed": True,
            },
        )
    )
    # Turn 5: booking succeeds -> LLM gives confirmation
    llm.add_response(text("Booked for Ada on November 2nd at 10:00 AM. See you then!"))

    transcript = run_conversation(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        user_inputs=[
            "I'd like to book for November 2nd",
            "10 AM",
            "Yes, that's right",
        ],
        llm_client=llm,
        db_path=db_path,
    )

    # Check the transcript
    assert len(transcript) == 6  # 3 user + 3 bot
    assert transcript[-1][1] == "Booked for Ada on November 2nd at 10:00 AM. See you then!"

    # Check database
    conn = connect(db_path)
    try:
        booking = conn.execute("SELECT * FROM bookings").fetchone()
        assert booking is not None
        assert booking["caller_name"] == "Ada"
        assert booking["status"] == "confirmed"
        # Cost logged
        cost = conn.execute(
            "SELECT * FROM eval_results WHERE eval_name = 'cost'"
        ).fetchone()
        assert cost is not None
        details = json.loads(cost["details_json"])
        assert details["prompt_tokens"] > 0
    finally:
        conn.close()


def test_booking_requires_confirmation(db_path):
    """Book without confirmed=true is rejected by validation."""
    llm = FakeLLMClient()
    # LLM tries to book without confirmed
    llm.add_response(
        tool_call(
            "book_slot",
            {
                "caller_name": "Ada",
                "caller_number": "(555) 123-4567",
                "date": "2026-11-02",
                "time": "10:00",
                "confirmed": False,
            },
        )
    )
    # Validation fails, LLM gets error, should ask for confirmation
    llm.add_response(
        text("I need your confirmation before booking. Please confirm the date and time.")
    )

    agent = make_agent(db_path, llm)
    reply = agent.receive("Book me at 10 AM tomorrow")
    assert "confirm" in reply.lower()


# ---------------------------------------------------------------------------
# Handoff triggers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason,message", [
    ("complaint", "I'm very unhappy with your service"),
    ("urgent", "This is an emergency, I need help now"),
    ("sensitive", "I want to delete all my data for legal reasons"),
    ("medical", "I have chest pains and need a doctor"),
    ("unsure", "I don't know what to do"),
    ("other", "Something else"),
])
def test_handoff_reasons(db_path, fake_llm, reason, message):
    """Each handoff reason writes a handoffs row."""
    fake_llm.add_response(
        tool_call(
            "handoff_to_human",
            {"reason": reason, "summary": message, "confirmed": True},
        )
    )
    agent = make_agent(db_path, fake_llm)
    agent.receive(message)
    conn = connect(db_path)
    try:
        handoff = conn.execute(
            "SELECT reason FROM handoffs WHERE call_id = ?",
            (agent.state.call_id,),
        ).fetchone()
        assert handoff is not None
        assert handoff["reason"] == reason
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------


def test_cost_logged_to_eval_results(db_path, fake_llm):
    """Token usage is accumulated and logged."""
    fake_llm.add_response(
        LLMResponse(
            content="Hello!",
            tool_calls=[],
            usage=LLMUsage(
                prompt_tokens=100, completion_tokens=50, total_tokens=150, model="fake"
            ),
            raw=None,
        )
    )
    agent = make_agent(db_path, fake_llm)
    agent.receive("Hi")
    agent.end_call()

    conn = connect(db_path)
    try:
        cost = conn.execute(
            "SELECT details_json FROM eval_results WHERE eval_name = 'cost'"
        ).fetchone()
        assert cost is not None
        details = json.loads(cost["details_json"])
        assert details["prompt_tokens"] == 100
        assert details["completion_tokens"] == 50
        # The model recorded is whatever the client reports.
        assert details["model"] == "fake"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reschedule and cancel
# ---------------------------------------------------------------------------


def test_reschedule_flow(db_path):
    """Reschedule: find booking -> check availability -> confirm -> reschedule."""
    # First, create a booking directly
    from app.tools import BookSlotInput, book_slot
    conn = connect(db_path)
    book_slot(
        conn,
        BookSlotInput(
            caller_name="Ada",
            caller_number="(555) 123-4567",
            date="2026-11-02",
            time="10:00",
            confirmed=True,
        ),
    )
    conn.close()

    llm = FakeLLMClient()
    llm.add_response(
        tool_call("check_availability", {"date": "2026-11-03", "confirmed": True})
    )
    llm.add_response(
        text(
            "Thursday at 2 PM is open. Let me confirm: moving from Nov 2 10 AM "
            "to Nov 3 2 PM. Correct?"
        )
    )
    llm.add_response(
        tool_call(
            "reschedule",
            {
                "caller_name": "Ada",
                "caller_number": "(555) 123-4567",
                "date": "2026-11-02",
                "time": "10:00",
                "new_date": "2026-11-03",
                "new_time": "14:00",
                "confirmed": True,
            },
        )
    )
    llm.add_response(
        text("Rescheduled from November 2nd at 10:00 AM to November 3rd at 2:00 PM.")
    )

    transcript = run_conversation(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        user_inputs=["Move my appointment to Thursday at 2 PM", "Yes"],
        llm_client=llm,
        db_path=db_path,
    )
    assert transcript

    conn = connect(db_path)
    try:
        booking = conn.execute("SELECT * FROM bookings").fetchone()
        # 2 PM EST = 19:00 UTC
        assert booking["slot_start"] == "2026-11-03T19:00:00+00:00"
    finally:
        conn.close()


def test_cancel_flow(db_path):
    """Cancel: find booking -> confirm -> cancel."""
    from app.tools import BookSlotInput, book_slot
    conn = connect(db_path)
    book_slot(
        conn,
        BookSlotInput(
            caller_name="Ada",
            caller_number="(555) 123-4567",
            date="2026-11-02",
            time="10:00",
            confirmed=True,
        ),
    )
    conn.close()

    llm = FakeLLMClient()
    llm.add_response(
        text(
            "Let me confirm: cancelling your appointment for November 2nd at "
            "10:00 AM. Is that right?"
        )
    )
    llm.add_response(
        tool_call(
            "cancel",
            {
                "caller_name": "Ada",
                "caller_number": "(555) 123-4567",
                "date": "2026-11-02",
                "time": "10:00",
                "confirmed": True,
            },
        )
    )
    llm.add_response(text("Cancelled your booking for November 2nd at 10:00 AM."))

    transcript = run_conversation(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        user_inputs=["Cancel my appointment", "Yes"],
        llm_client=llm,
        db_path=db_path,
    )
    assert transcript

    conn = connect(db_path)
    try:
        booking = conn.execute("SELECT * FROM bookings").fetchone()
        assert booking["status"] == "cancelled"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool error handling
# ---------------------------------------------------------------------------


def test_tool_timeout_retried(db_path):
    """Timeout from calendar is retried (fake LLM simulates retry)."""
    from app.tools.calendar import MockCalendarClient, set_calendar_client

    set_calendar_client(MockCalendarClient(failure_mode="timeout", failure_times=1))
    book_args = json.dumps({
        "caller_name": "Ada",
        "caller_number": "(555) 123-4567",
        "date": "2026-11-02",
        "time": "10:00",
        "confirmed": True,
    })
    try:
        llm = FakeLLMClient()
        llm.add_response(
            tool_call("book_slot", raw_args=book_args)
        )
        llm.add_response(
            text("The booking system didn't respond. Let me try again.")
        )
        llm.add_response(
            tool_call("book_slot", raw_args=book_args)
        )
        llm.add_response(
            text("Booked for Ada on November 2nd at 10:00 AM.")
        )

        transcript = run_conversation(
            caller_number="(555) 123-4567",
            caller_number_masked="(XXX) XXX-4567",
            user_inputs=["Book me at 10 AM", "Yes"],
            llm_client=llm,
            db_path=db_path,
        )
        assert "booked" in transcript[-1][1].lower()
    finally:
        set_calendar_client(None)


# ---------------------------------------------------------------------------
# run_conversation helper
# ---------------------------------------------------------------------------


def test_run_conversation_returns_transcript(db_path, fake_llm):
    """run_conversation returns (speaker, text) pairs."""
    fake_llm.add_response(text("Hello!"))
    transcript = run_conversation(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        user_inputs=["Hi"],
        llm_client=fake_llm,
        db_path=db_path,
    )
    assert transcript == [("caller", "Hi"), ("bot", "Hello!")]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_agent_config_from_env():
    """AgentConfig reads from settings."""
    config = AgentConfig.from_env()
    assert config.max_steps == 10
    assert config.call_timeout_s == 30.0