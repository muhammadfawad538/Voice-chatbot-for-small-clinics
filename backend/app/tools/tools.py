"""Tool input models and the tool functions for Phase 2a.

Every tool function is decorated with :func:`tool` and receives a database
connection plus its input model. Input models carry no failure-mode field so
the LLM cannot steer the system via it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.tools.calendar import get_calendar_client
from app.tools.decorator import tool
from app.tools.masking import mask_phone

# ---------------------------------------------------------------------------
# Tool input models
# ---------------------------------------------------------------------------


class HandoffReason(StrEnum):
    COMPLAINT = "complaint"
    URGENT = "urgent"
    SENSITIVE = "sensitive"
    MEDICAL = "medical"
    UNSURE = "unsure"
    OTHER = "other"


def _idempotency_key(caller_name: str, caller_number: str, date: str, time: str) -> str:
    """Derive a deterministic key from the booking details.

    The raw number goes into the hash and nowhere else.
    """
    raw = f"{caller_name}|{caller_number}|{date}|{time}"
    return "bk_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


class CheckAvailabilityInput(BaseModel):
    date: str = Field(description="Business-local date, YYYY-MM-DD")
    confirmed: bool = Field(
        default=True,
        description="Required for all tools; must be True even for read tools",
    )

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        return v


class BookSlotInput(BaseModel):
    caller_name: str = Field(min_length=1, max_length=100)
    caller_number: str = Field(min_length=10, max_length=20)
    date: str = Field(description="Business-local date, YYYY-MM-DD")
    time: str = Field(description="Business-local time, HH:MM (24h)")
    confirmed: bool = Field(
        default=False,
        description=(
            "Must be True to actually book. The LLM sets this "
            "after reading back the details."
        ),
    )

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        return v

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError as exc:
            raise ValueError("time must be HH:MM") from exc
        return v


class RescheduleInput(BaseModel):
    caller_name: str = Field(min_length=1, max_length=100)
    caller_number: str = Field(min_length=10, max_length=20)
    date: str | None = Field(default=None, description="Current booking date, YYYY-MM-DD")
    time: str | None = Field(default=None, description="Current booking time, HH:MM")
    new_date: str = Field(description="New business-local date, YYYY-MM-DD")
    new_time: str = Field(description="New business-local time, HH:MM")
    confirmed: bool = Field(default=False)

    @field_validator("date", "new_date")
    @classmethod
    def _valid_dates(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        return v

    @field_validator("time", "new_time")
    @classmethod
    def _valid_times(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError as exc:
            raise ValueError("time must be HH:MM") from exc
        return v


class CancelInput(BaseModel):
    caller_name: str = Field(min_length=1, max_length=100)
    caller_number: str = Field(min_length=10, max_length=20)
    date: str | None = Field(default=None, description="Booking date, YYYY-MM-DD")
    time: str | None = Field(default=None, description="Booking time, HH:MM")
    confirmed: bool = Field(default=False)

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        return v

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError as exc:
            raise ValueError("time must be HH:MM") from exc
        return v


class HandoffToHumanInput(BaseModel):
    reason: HandoffReason
    summary: str = Field(min_length=1, max_length=500)
    confirmed: bool = Field(
        default=False,
        description="The agent confirms it wants to hand off. The caller does not agree to this.",
    )


# ---------------------------------------------------------------------------
# Timezone & business-hours helpers
# ---------------------------------------------------------------------------


def _load_calendar_config() -> dict[str, Any]:
    """Load calendar configuration from settings."""
    settings = get_settings()
    return {
        "timezone": settings.calendar_timezone,
        "open_time": settings.calendar_open_time,
        "close_time": settings.calendar_close_time,
        "slot_minutes": settings.calendar_slot_minutes,
        "closed_weekdays": settings.calendar_closed_weekdays,
    }


def _parse_closed_weekdays(text: str) -> set[str]:
    return {day.strip().lower() for day in text.split(",") if day.strip()}


def _is_closed_day(date_str: str, config: dict[str, Any]) -> bool:
    """True when the business is closed on the given business-local date."""
    date = datetime.strptime(date_str, "%Y-%m-%d")
    return date.strftime("%A").lower() in _parse_closed_weekdays(config["closed_weekdays"])


def _local_to_utc(date_str: str, time_str: str, config: dict[str, Any]) -> datetime:
    """Convert business-local date+time to a UTC datetime."""
    tz = ZoneInfo(config["timezone"])
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    return dt.astimezone(UTC)


def _utc_to_local(utc_dt: datetime, config: dict[str, Any]) -> tuple[str, str]:
    """Convert a UTC datetime to business-local (date, time)."""
    tz = ZoneInfo(config["timezone"])
    local = utc_dt.astimezone(tz)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _business_day_window(date_str: str, config: dict[str, Any]) -> tuple[datetime, datetime]:
    """Return (utc_open, utc_close) for the given business-local date."""
    open_h, open_m = map(int, config["open_time"].split(":"))
    close_h, close_m = map(int, config["close_time"].split(":"))

    date = datetime.strptime(date_str, "%Y-%m-%d")
    tz = ZoneInfo(config["timezone"])
    dt_open = datetime(date.year, date.month, date.day, open_h, open_m, tzinfo=tz)
    dt_close = datetime(date.year, date.month, date.day, close_h, close_m, tzinfo=tz)
    return dt_open.astimezone(UTC), dt_close.astimezone(UTC)


def _slot_within_hours(
    date_str: str, time_str: str, config: dict[str, Any]
) -> tuple[datetime, datetime] | None:
    """Return (utc_start, utc_end) if the slot sits inside business hours.

    ``None`` means the slot is outside opening hours.
    """
    slot_start = _local_to_utc(date_str, time_str, config)
    slot_end = slot_start + timedelta(minutes=config["slot_minutes"])
    utc_open, utc_close = _business_day_window(date_str, config)
    if slot_start < utc_open or slot_end > utc_close:
        return None
    return slot_start, slot_end


def _find_booking(
    conn: sqlite3.Connection,
    caller_name: str,
    caller_number_masked: str,
    date: str | None,
    time: str | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Find a caller's booking.

    With a date and time, the match is exact. Without them, the caller's most
    recent confirmed booking is used.
    """
    if date is not None and time is not None:
        slot_start_utc = _local_to_utc(date, time, config)
        row = conn.execute(
            """
            SELECT * FROM bookings
            WHERE caller_name = ? AND caller_number_masked = ?
              AND slot_start = ?
              AND status = 'confirmed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (caller_name, caller_number_masked, slot_start_utc.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    row = conn.execute(
        """
        SELECT * FROM bookings
        WHERE caller_name = ? AND caller_number_masked = ? AND status = 'confirmed'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (caller_name, caller_number_masked),
    ).fetchone()
    return dict(row) if row else None


def _error(error_code: str, message: str) -> dict[str, Any]:
    """Build the error dict the decorator turns into a failed ToolResult."""
    return {"error": True, "error_code": error_code, "message": message}


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


@tool
def check_availability(conn, inp: CheckAvailabilityInput) -> dict[str, Any]:
    """Return free slots for the given date as local HH:MM strings."""
    if not inp.confirmed:
        return _error(
            "confirmation_required", "Please confirm you want to check availability."
        )

    config = _load_calendar_config()
    if _is_closed_day(inp.date, config):
        return {"slots": [], "message": "We are closed on that day."}

    utc_open, utc_close = _business_day_window(inp.date, config)
    client = get_calendar_client()
    slots = client.check_availability(utc_open, utc_close)

    tz = ZoneInfo(config["timezone"])
    local_slots = []
    for slot in slots:
        start = datetime.fromisoformat(slot["start"]).astimezone(tz)
        local_slots.append(start.strftime("%H:%M"))

    return {"slots": local_slots, "date": inp.date}


@tool
def book_slot(conn, inp: BookSlotInput) -> dict[str, Any]:
    """Book a slot for the caller. Requires confirmed=True."""
    if not inp.confirmed:
        return _error(
            "confirmation_required",
            "I need your confirmation before booking. Please confirm the date and time.",
        )

    config = _load_calendar_config()
    if _is_closed_day(inp.date, config):
        return _error("invalid_arguments", "We are closed on that day.")

    window = _slot_within_hours(inp.date, inp.time, config)
    if window is None:
        return _error("invalid_arguments", "That time is outside business hours.")
    slot_start_utc, slot_end_utc = window

    masked = mask_phone(inp.caller_number)
    key = _idempotency_key(inp.caller_name, inp.caller_number, inp.date, inp.time)

    client = get_calendar_client()
    booking = client.book_slot(
        caller_name=inp.caller_name,
        caller_number_masked=masked,
        slot_start=slot_start_utc,
        slot_end=slot_end_utc,
        idempotency_key=key,
    )

    local_date, local_time = _utc_to_local(slot_start_utc, config)
    return {
        **booking,
        "display_date": local_date,
        "display_time": local_time,
        "message": f"Booked for {inp.caller_name} on {local_date} at {local_time}.",
    }


@tool
def reschedule(conn, inp: RescheduleInput) -> dict[str, Any]:
    """Reschedule the caller's booking to a new slot. Requires confirmed=True."""
    if not inp.confirmed:
        return _error(
            "confirmation_required",
            "I need your confirmation before rescheduling. Please confirm the new date and time.",
        )

    config = _load_calendar_config()
    masked = mask_phone(inp.caller_number)

    existing = _find_booking(conn, inp.caller_name, masked, inp.date, inp.time, config)
    if existing is None:
        return _error("not_found", "I couldn't find a matching booking to reschedule.")

    if _is_closed_day(inp.new_date, config):
        return _error("invalid_arguments", "We are closed on that day.")

    window = _slot_within_hours(inp.new_date, inp.new_time, config)
    if window is None:
        return _error("invalid_arguments", "That time is outside business hours.")
    new_start_utc, new_end_utc = window

    client = get_calendar_client()
    booking = client.reschedule(
        booking_id=existing["id"],
        slot_start=new_start_utc,
        slot_end=new_end_utc,
    )

    local_date, local_time = _utc_to_local(new_start_utc, config)
    old_date, old_time = _utc_to_local(datetime.fromisoformat(existing["slot_start"]), config)

    return {
        **booking,
        "display_date": local_date,
        "display_time": local_time,
        "old_display_date": old_date,
        "old_display_time": old_time,
        "message": f"Rescheduled from {old_date} at {old_time} to {local_date} at {local_time}.",
    }


@tool
def cancel(conn, inp: CancelInput) -> dict[str, Any]:
    """Cancel the caller's booking. Requires confirmed=True."""
    if not inp.confirmed:
        return _error(
            "confirmation_required",
            "I need your confirmation before cancelling. "
            "Please confirm you want to cancel this booking.",
        )

    config = _load_calendar_config()
    masked = mask_phone(inp.caller_number)

    existing = _find_booking(conn, inp.caller_name, masked, inp.date, inp.time, config)
    if existing is None:
        return _error("not_found", "I couldn't find a matching booking to cancel.")

    client = get_calendar_client()
    booking = client.cancel(booking_id=existing["id"])

    local_date, local_time = _utc_to_local(datetime.fromisoformat(existing["slot_start"]), config)
    return {
        **booking,
        "display_date": local_date,
        "display_time": local_time,
        "message": f"Cancelled your booking for {local_date} at {local_time}.",
    }


@tool
def handoff_to_human(conn, inp: HandoffToHumanInput) -> dict[str, Any]:
    """Escalate to a human. Writes a handoffs row. Requires confirmed=True."""
    if not inp.confirmed:
        return _error("confirmation_required", "I need to confirm before transferring you.")

    # A handoff belongs to a call. The agent loop passes the call id in via
    # _call_id; outside a call the row simply has no call_id.
    call_id = None
    conn.execute(
        """
        INSERT INTO handoffs (call_id, reason, status, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        (call_id, inp.reason.value, _utcnow()),
    )
    conn.commit()

    return {
        "reason": inp.reason.value,
        "summary": inp.summary,
        "message": "I'm transferring you to a human. Please hold.",
    }


def _utcnow() -> str:
    from app.db import utcnow

    return utcnow()


__all__ = [
    "check_availability",
    "book_slot",
    "reschedule",
    "cancel",
    "handoff_to_human",
    "CheckAvailabilityInput",
    "BookSlotInput",
    "RescheduleInput",
    "CancelInput",
    "HandoffToHumanInput",
]
