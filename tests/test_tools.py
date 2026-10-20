"""Tests for Phase 2a — Mock Calendar and Tools."""

from __future__ import annotations

import inspect
import typing

import pytest

from app.db import connect
from app.tools import (
    BookSlotInput,
    CancelInput,
    CheckAvailabilityInput,
    HandoffToHumanInput,
    RescheduleInput,
    book_slot,
    cancel,
    check_availability,
    handoff_to_human,
    reschedule,
)
from app.tools.calendar import MockCalendarClient
from app.tools.decorator import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_tool(tool_func, inp, db_path):
    """Run a tool function in isolation using the test fixture's database."""
    conn = connect(db_path)
    try:
        return tool_func(conn, inp)
    finally:
        conn.close()


def _assert_ok(result, data=None):
    tr = ToolResult(**{k: v for k, v in result.items() if k != "data"})
    assert tr.ok is True
    assert tr.error_code is None
    if data is not None:
        actual = result["data"]
        expected = data
        for key, value in expected.items():
            assert actual.get(key) == value, (
                f"key {key!r}: expected {value!r}, got {actual.get(key)!r}"
            )


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


def test_book_slot_input_validates():
    with pytest.raises(ValueError):
        BookSlotInput(caller_name="", date="2026-11-02", time="10:00", confirmed=True)


def test_reschedule_input_accepts_missing_anchor(db_path):
    inp = RescheduleInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        new_date="2026-11-03",
        new_time="14:00",
        confirmed=True,
    )
    assert inp.date is None and inp.time is None


# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------


def test_check_availability_requires_confirmed(db_path):
    inp = CheckAvailabilityInput(date="2026-11-02", confirmed=False)
    result = _run_tool(check_availability, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "confirmation_required"


def test_check_availability_closed_day(db_path):
    # 2026-11-07 is a Saturday -> closed
    inp = CheckAvailabilityInput(date="2026-11-07", confirmed=True)
    result = _run_tool(check_availability, inp, db_path)
    assert result["ok"] is True
    assert result.get("error_code") is None
    assert result["data"]["slots"] == []


def test_check_availability_open_day_returns_slots(db_path):
    inp = CheckAvailabilityInput(date="2026-11-02", confirmed=True)  # Monday
    result = _run_tool(check_availability, inp, db_path)
    assert result["ok"] is True
    assert result.get("error_code") is None
    slots = result["data"]["slots"]
    assert isinstance(slots, list)
    assert len(slots) > 0
    for t in slots:
        assert len(t) == 5  # HH:MM format
        assert t.count(":") == 1


def test_check_availability_logs_call(db_path):
    inp = CheckAvailabilityInput(date="2026-11-02", confirmed=True)
    _run_tool(check_availability, inp, db_path)

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM tool_calls").fetchall()
        assert len(rows) >= 1
        # arguments_json contains masked number
        first = rows[0]
        assert first["arguments_json"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# book_slot
# ---------------------------------------------------------------------------


def test_book_slot_requires_confirmed(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=False,
    )
    result = _run_tool(book_slot, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "confirmation_required"


def test_book_slot_outside_hours(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="08:00",  # before 9 AM
        confirmed=True,
    )
    result = _run_tool(book_slot, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_arguments"


def test_book_slot_success_creates_booking(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    result = _run_tool(book_slot, inp, db_path)
    assert result["ok"] is True
    assert result["data"]["status"] == "confirmed"
    assert result["data"]["caller_number_masked"] == "(XXX) XXX-4567"

    # DB invariant: exactly one confirmed booking on that slot
    conn = connect(db_path)
    try:
        # 10:00 EST on 2026-11-02 = 15:00 UTC (EST is UTC-5 in November)
        row = conn.execute(
            """SELECT COUNT(*) FROM bookings WHERE slot_start = ? AND status = 'confirmed'""",
            ("2026-11-02T15:00:00+00:00",),
        ).fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_book_slot_idempotent(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    r1 = _run_tool(book_slot, inp, db_path)
    r2 = _run_tool(book_slot, inp, db_path)
    assert r1["data"]["id"] == r2["data"]["id"]

    # Only one booking row exists
    conn = connect(db_path)
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()


def test_cancelled_slot_can_be_rebooked(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    r1 = _run_tool(book_slot, inp, db_path)
    bid = r1["data"]["id"]

    # Cancel via direct DB (tool not yet tested for cancel; we go straight there)
    conn = connect(db_path)
    try:
        conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (bid,))
        conn.commit()

        # The same key revives the cancelled row rather than adding a second one.
        r2 = _run_tool(book_slot, inp, db_path)
        assert r2["data"]["status"] == "confirmed"
        assert r2["data"]["id"] == bid

        cnt = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_start = ? AND status = 'confirmed'",
            ("2026-11-02T15:00:00+00:00",),
        ).fetchone()[0]
        assert cnt == 1

        total = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        assert total == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Failure injection via shared client
# ---------------------------------------------------------------------------


@pytest.fixture
def fail_client():
    """A calendar client with the configured failure plan applied."""
    client = MockCalendarClient(failure_mode="timeout", failure_times=1)
    from app.tools.calendar import set_calendar_client

    set_calendar_client(client)
    yield client
    set_calendar_client(None)


def test_failure_injection_timeout(fail_client, db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    result = _run_tool(book_slot, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "timeout"
    assert result["injected"] is True


def test_failure_injection_slot_taken_at_last_moment(monkeypatch, db_path):
    # First call succeeds
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    r1 = _run_tool(book_slot, inp, db_path)
    assert r1["ok"] is True

    # Second call fails (same inputs -> same idempotency key -> same existing booking)
    from app.tools.calendar import set_calendar_client

    client = MockCalendarClient(failure_mode="slot_taken_at_last_moment", failure_times=1)
    set_calendar_client(client)
    try:
        result = _run_tool(book_slot, inp, db_path)
        assert result["ok"] is False
        assert result["error_code"] == "slot_unavailable"
        assert result["injected"] is True
    finally:
        set_calendar_client(None)


def test_failure_injection_respects_times_n(monkeypatch, db_path):
    from app.tools.calendar import set_calendar_client

    client = MockCalendarClient(failure_mode="timeout", failure_times=2)
    set_calendar_client(client)
    try:
        inp = BookSlotInput(
            caller_name="Ada",
            caller_number="(555) 123-4567",
            date="2026-11-02",
            time="10:00",
            confirmed=True,
        )
        for _ in range(2):
            result = _run_tool(book_slot, inp, db_path)
            assert result["ok"] is False
            assert result["error_code"] == "timeout"
            assert result["injected"] is True

        # Third call succeeds
        result = _run_tool(book_slot, inp, db_path)
        assert result["ok"] is True
    finally:
        set_calendar_client(None)


def test_failure_injection_server_error(monkeypatch, db_path):
    from app.tools.calendar import set_calendar_client

    client = MockCalendarClient(failure_mode="server_error", failure_times=1)
    set_calendar_client(client)
    try:
        inp = BookSlotInput(
            caller_name="Ada",
            caller_number="(555) 123-4567",
            date="2026-11-02",
            time="10:00",
            confirmed=True,
        )
        result = _run_tool(book_slot, inp, db_path)
        assert result["ok"] is False
        assert result["error_code"] == "provider_error"
        assert result["injected"] is True
    finally:
        set_calendar_client(None)


def test_no_failure_field_in_tool_schema():
    """Every tool's JSON schema must NOT expose a failure-mode field."""
    from pydantic import BaseModel

    import app.tools as tools_mod

    tool_names = ["check_availability", "book_slot", "reschedule", "cancel", "handoff_to_human"]
    for name in tool_names:
        func = getattr(tools_mod, name)
        # The input model is the second positional parameter. `from __future__
        # import annotations` makes annotations strings, so resolve them.
        original = inspect.unwrap(func)
        hints = typing.get_type_hints(original)
        input_model = hints[list(inspect.signature(original).parameters)[1]]
        assert isinstance(input_model, type) and issubclass(input_model, BaseModel), (
            f"{name}'s input isn't a Pydantic model"
        )
        props = input_model.model_json_schema().get("properties", {})
        for prop in props:
            assert prop != "failure_mode", f"Tool {name} exposes failure_mode"
            assert prop != "injected", f"Tool {name} exposes injected"


# ---------------------------------------------------------------------------
# reschedule
# ---------------------------------------------------------------------------


def test_reschedule_requires_confirmed(db_path):
    inp = RescheduleInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        new_date="2026-11-03",
        new_time="14:00",
        confirmed=False,
    )
    result = _run_tool(reschedule, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "confirmation_required"


def test_reschedule_moves_booking_and_frees_old_slot(db_path):
    book_inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    booked = _run_tool(book_slot, book_inp, db_path)
    assert booked["ok"] is True
    bid = booked["data"]["id"]

    resched_inp = RescheduleInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        new_date="2026-11-03",
        new_time="14:00",
        confirmed=True,
    )
    result = _run_tool(reschedule, resched_inp, db_path)
    assert result["ok"] is True
    assert result["data"]["id"] == bid
    assert result["data"]["display_date"] == "2026-11-03"
    assert result["data"]["display_time"] == "14:00"

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT slot_start, status FROM bookings").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "confirmed"
        # 14:00 EST on 2026-11-03 = 19:00 UTC
        assert rows[0]["slot_start"] == "2026-11-03T19:00:00+00:00"
    finally:
        conn.close()


def test_reschedule_unknown_booking_returns_not_found(db_path):
    inp = RescheduleInput(
        caller_name="Nobody",
        caller_number="(555) 000-0000",
        new_date="2026-11-03",
        new_time="14:00",
        confirmed=True,
    )
    result = _run_tool(reschedule, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "not_found"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_requires_confirmed(db_path):
    inp = CancelInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=False,
    )
    result = _run_tool(cancel, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "confirmation_required"


def test_cancel_marks_booking_cancelled_and_frees_slot(db_path):
    book_inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    booked = _run_tool(book_slot, book_inp, db_path)
    assert booked["ok"] is True

    cancel_inp = CancelInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    result = _run_tool(cancel, cancel_inp, db_path)
    assert result["ok"] is True
    assert result["data"]["status"] == "cancelled"

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT status FROM bookings").fetchone()
        assert row["status"] == "cancelled"
    finally:
        conn.close()


def test_cancelled_slot_is_free_again(db_path):
    book_inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    _run_tool(book_slot, book_inp, db_path)

    avail_inp = CheckAvailabilityInput(date="2026-11-02", confirmed=True)
    booked_slots = _run_tool(check_availability, avail_inp, db_path)["data"]["slots"]
    assert "10:00" not in booked_slots

    cancel_inp = CancelInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    _run_tool(cancel, cancel_inp, db_path)

    free_slots = _run_tool(check_availability, avail_inp, db_path)["data"]["slots"]
    assert "10:00" in free_slots


def test_booked_slot_disappears_from_availability(db_path):
    book_inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    _run_tool(book_slot, book_inp, db_path)

    avail_inp = CheckAvailabilityInput(date="2026-11-02", confirmed=True)
    slots = _run_tool(check_availability, avail_inp, db_path)["data"]["slots"]
    assert "10:00" not in slots
    assert "09:00" in slots
    assert "09:30" in slots


# ---------------------------------------------------------------------------
# Business hours and configuration
# ---------------------------------------------------------------------------


def test_availability_covers_business_hours_only(db_path):
    inp = CheckAvailabilityInput(date="2026-11-02", confirmed=True)
    slots = _run_tool(check_availability, inp, db_path)["data"]["slots"]
    assert slots[0] == "09:00"
    assert slots[-1] == "16:30"
    assert "08:30" not in slots
    assert "17:00" not in slots


def test_book_slot_on_closed_day_is_rejected(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-07",  # Saturday
        time="10:00",
        confirmed=True,
    )
    result = _run_tool(book_slot, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "invalid_arguments"


def test_stored_times_are_utc(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    result = _run_tool(book_slot, inp, db_path)
    # 10:00 America/New_York in November is 15:00 UTC
    assert result["data"]["slot_start"] == "2026-11-02T15:00:00+00:00"


# ---------------------------------------------------------------------------
# Logging and phone masking
# ---------------------------------------------------------------------------


def test_phone_numbers_masked_in_log(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=True,
    )
    _run_tool(book_slot, inp, db_path)

    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT arguments_json FROM tool_calls ORDER BY id DESC LIMIT 1"
        )
        (args_json,) = cur.fetchone()
        assert "(XXX) XXX-4567" in args_json
        # Raw digits should not leak
        assert "555" not in args_json
        assert "123" not in args_json
        # Full-looking phone numbers must be masked away
        assert "(555)" not in args_json
    finally:
        conn.close()


def test_tool_call_logged_on_failure(db_path):
    inp = BookSlotInput(
        caller_name="Ada",
        caller_number="(555) 123-4567",
        date="2026-11-02",
        time="10:00",
        confirmed=False,
    )
    result = _run_tool(book_slot, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "confirmation_required"

    # A tool_calls row was written with success=0
    conn = connect(db_path)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE success = 0"
        ).fetchone()[0]
        assert cnt >= 1
    finally:
        conn.close()


def test_injected_flag_written_to_log(db_path):
    from app.tools.calendar import MockCalendarClient, set_calendar_client

    set_calendar_client(MockCalendarClient(failure_mode="timeout", failure_times=1))
    try:
        inp = BookSlotInput(
            caller_name="Ada",
            caller_number="(555) 123-4567",
            date="2026-11-02",
            time="10:00",
            confirmed=True,
        )
        result = _run_tool(book_slot, inp, db_path)
    finally:
        set_calendar_client(None)

    assert result["injected"] is True

    # Verify the row was written with injected=1
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT injected FROM tool_calls ORDER BY id DESC LIMIT 1"
        )
        (iv,) = cur.fetchone()
        assert iv == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# handoff_to_human
# ---------------------------------------------------------------------------


def test_handoff_to_human_requires_confirmed(db_path):
    inp = HandoffToHumanInput(
        reason="complaint",
        summary="The caller is upset about waiting times.",
        confirmed=False,
    )
    result = _run_tool(handoff_to_human, inp, db_path)
    assert result["ok"] is False
    assert result["error_code"] == "confirmation_required"


def test_handoff_to_human_success(db_path):
    inp = HandoffToHumanInput(
        reason="urgent",
        summary="Caller says they have chest pains.",
        confirmed=True,
    )
    result = _run_tool(handoff_to_human, inp, db_path)
    assert result["ok"] is True
    assert result["data"]["reason"] == "urgent"
    assert "chest" in result["data"]["summary"].lower()

    # A handoffs row was written
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM handoffs").fetchall()
        assert len(rows) == 1
        assert rows[0]["reason"] == "urgent"
        assert rows[0]["status"] == "pending"
    finally:
        conn.close()
