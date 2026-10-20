"""Calendar interface and mock implementation for Phase 2a.

Tools call :class:`CalendarClient` and never the mock. The interface is the
seam Phase 6 uses: a Cal.com or Google Calendar client is written against the
same four methods and substituted, with no change to tool or agent code.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.db import BookingConflictError, create_booking
from app.db import connect as db_connect

# ---------------------------------------------------------------------------
# Calendar exceptions
# ---------------------------------------------------------------------------


class CalendarError(Exception):
    """Base exception for calendar operations."""

    def __init__(self, message: str = "calendar error", *, injected: bool = False):
        super().__init__(message)
        self.injected = injected


class CalendarTimeoutError(CalendarError):
    """The provider did not respond in time."""


class CalendarProviderError(CalendarError):
    """The provider failed (5xx, network error)."""


class CalendarSlotUnavailableError(CalendarError):
    """The slot is no longer free (double-booking)."""


class CalendarNotFoundError(CalendarError):
    """No booking with the given id exists."""


# ---------------------------------------------------------------------------
# CalendarClient — the interface the tools are only allowed to call
# ---------------------------------------------------------------------------


class CalendarClient(ABC):
    """The calendar contract that every tool must call."""

    @abstractmethod
    def check_availability(
        self, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        """Return available slots inside the window.

        ``window_start`` / ``window_end`` are UTC datetimes. The mock builds a
        business-hour slot grid inside the window and marks each slot free or
        busy.
        """
        ...

    @abstractmethod
    def book_slot(
        self,
        *,
        caller_name: str,
        caller_number_masked: str,
        slot_start: datetime,
        slot_end: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Book a slot and return a booking dict."""
        ...

    @abstractmethod
    def reschedule(
        self,
        *,
        booking_id: int,
        slot_start: datetime,
        slot_end: datetime,
    ) -> dict[str, Any]:
        """Reschedule a booking. Returns the updated booking dict."""
        ...

    @abstractmethod
    def cancel(self, *, booking_id: int) -> dict[str, Any]:
        """Cancel a booking. Returns the updated booking dict."""
        ...


# ---------------------------------------------------------------------------
# MockCalendarClient — implementation on top of SQLite + create_booking
# ---------------------------------------------------------------------------


class MockCalendarClient(CalendarClient):
    """CalendarClient backed by SQLite and the existing ``create_booking``."""

    def __init__(
        self,
        *,
        tz_name: str | None = None,
        failure_mode: str | None = None,
        failure_times: int | None = None,
    ):
        settings = get_settings()
        self._tz = tz_name or settings.calendar_timezone
        self._slot_minutes = settings.calendar_slot_minutes

        # operation -> (mode, times, calls_so_far). A plan is per-operation so
        # a test can mix modes: book_slot times out while check_availability
        # 500s.
        self._plans: dict[str, tuple[str, int | None, int]] = {}

        # A plan passed to the constructor, or inherited from settings, applies
        # to every operation. A per-operation plan set with set_failure() wins.
        mode = failure_mode if failure_mode is not None else settings.calendar_failure_mode
        times = failure_times if failure_times is not None else settings.calendar_failure_times
        if mode is not None:
            self._plans["*"] = (mode, times, 0)

    # -- failure plan -----------------------------------------------------

    def set_failure(
        self,
        operation: str,
        mode: str,
        times: int | None = None,
    ) -> None:
        """Configure the failure plan for one operation.

        operation — check_availability, book_slot, reschedule, cancel, or "*"
                    for all of them.
        mode     — timeout, server_error, slot_taken_at_last_moment.
        times    — None means "always"; an int means "fail the next N calls,
                    then succeed".
        """
        valid_ops = {
            "check_availability",
            "book_slot",
            "reschedule",
            "cancel",
            "*",
        }
        valid_modes = {"timeout", "server_error", "slot_taken_at_last_moment"}
        if operation not in valid_ops:
            raise ValueError(f"unknown operation: {operation}")
        if mode not in valid_modes:
            raise ValueError(f"unknown failure mode: {mode}")
        if mode == "slot_taken_at_last_moment" and operation not in ("book_slot", "*"):
            raise ValueError("slot_taken_at_last_moment only applies to book_slot")
        if times is not None and times < 0:
            raise ValueError("times must be non-negative")

        self._plans[operation] = (mode, times, 0)

    def clear_failure(self, operation: str | None = None) -> None:
        """Remove the failure plan for one operation, or for all of them."""
        if operation is None:
            self._plans.clear()
        else:
            self._plans.pop(operation, None)

    def _check_failure(self, operation: str) -> None:
        """Raise if this operation has failure injection configured.

        A plan set for the specific operation wins over the "*" plan. Within a
        plan, ``times=None`` means always fail; an integer means the next N
        calls fail and the ones after succeed.
        """
        key = operation if operation in self._plans else "*"
        plan = self._plans.get(key)
        if plan is None:
            return

        mode, times, calls_so_far = plan
        should_fail = True if times is None else calls_so_far < times

        # Count the call either way, so "fail the next N" advances.
        self._plans[key] = (mode, times, calls_so_far + 1)

        if not should_fail:
            return

        if mode == "slot_taken_at_last_moment":
            raise CalendarSlotUnavailableError("slot unavailable at last moment", injected=True)
        if mode == "server_error":
            raise CalendarProviderError("calendar provider error", injected=True)
        raise CalendarTimeoutError("calendar timeout", injected=True)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _to_utc(dt: datetime) -> datetime:
        """Return a UTC-aware datetime, assuming UTC for a naive input."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    @staticmethod
    def _parse_iso(text: str) -> datetime:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    def _local(self, dt: datetime) -> datetime:
        """Convert a datetime to the business timezone."""
        from zoneinfo import ZoneInfo

        return self._to_utc(dt).astimezone(ZoneInfo(self._tz))

    @staticmethod
    def _row_to_booking(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "caller_name": row["caller_name"],
            "caller_number_masked": row["caller_number_masked"],
            "slot_start": row["slot_start"],
            "slot_end": row["slot_end"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }

    def _busy_intervals(self) -> list[tuple[datetime, datetime]]:
        """Confirmed bookings as (start, end) UTC datetime pairs."""
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT slot_start, slot_end FROM bookings WHERE status = 'confirmed'"
            ).fetchall()
        finally:
            conn.close()
        return [(self._parse_iso(r["slot_start"]), self._parse_iso(r["slot_end"])) for r in rows]

    # -- calendar operations ----------------------------------------------

    def check_availability(
        self, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        """Free slots inside the window, as UTC ISO start/end pairs.

        The grid is built in business-local time so slots line up with the
        clinic's opening hour even when the UTC offset is not a whole hour.
        """
        self._check_failure("check_availability")

        ws = self._to_utc(window_start)
        we = self._to_utc(window_end)
        if we <= ws:
            return []

        busy = self._busy_intervals()
        step = timedelta(minutes=self._slot_minutes)

        # Build the grid in local time, then convert each slot back to UTC.
        local_start = self._local(ws)
        local_end = self._local(we)

        slots: list[dict[str, Any]] = []
        cur = local_start
        while cur + step <= local_end:
            slot_start_utc = self._to_utc(cur)
            slot_end_utc = slot_start_utc + step

            overlaps = any(
                b_start < slot_end_utc and b_end > slot_start_utc for b_start, b_end in busy
            )
            if not overlaps:
                slots.append(
                    {
                        "start": slot_start_utc.isoformat(),
                        "end": slot_end_utc.isoformat(),
                    }
                )
            cur += step

        return slots

    def book_slot(
        self,
        *,
        caller_name: str,
        caller_number_masked: str,
        slot_start: datetime,
        slot_end: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Book a slot. Idempotent on ``idempotency_key``."""
        self._check_failure("book_slot")
        try:
            row = create_booking(
                conn=None,
                caller_name=caller_name,
                caller_number_masked=caller_number_masked,
                slot_start=self._to_utc(slot_start).isoformat(),
                slot_end=self._to_utc(slot_end).isoformat(),
                idempotency_key=idempotency_key,
            )
        except BookingConflictError as exc:
            raise CalendarSlotUnavailableError(str(exc)) from exc
        return self._row_to_booking(row)

    def reschedule(
        self,
        *,
        booking_id: int,
        slot_start: datetime,
        slot_end: datetime,
    ) -> dict[str, Any]:
        """Move a booking to a new slot, keeping its id.

        The booking's ``idempotency_key`` is left alone: it identifies the
        booking, and rewriting it would break the retry guarantee the key
        exists to provide.
        """
        self._check_failure("reschedule")
        new_start = self._to_utc(slot_start).isoformat()
        new_end = self._to_utc(slot_end).isoformat()

        conn = db_connect()
        try:
            existing = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if existing is None:
                raise CalendarNotFoundError(f"booking {booking_id} not found")
            if existing["status"] == "cancelled":
                raise CalendarNotFoundError(f"booking {booking_id} is cancelled")

            try:
                conn.execute(
                    """
                    UPDATE bookings
                    SET slot_start = ?, slot_end = ?, status = 'confirmed'
                    WHERE id = ?
                    """,
                    (new_start, new_end, booking_id),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise CalendarSlotUnavailableError(
                    f"slot {new_start} is already confirmed by another booking"
                ) from exc

            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            return self._row_to_booking(row)
        finally:
            conn.close()

    def cancel(self, *, booking_id: int) -> dict[str, Any]:
        """Cancel a booking, freeing its slot for rebooking."""
        self._check_failure("cancel")
        conn = db_connect()
        try:
            existing = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if existing is None:
                raise CalendarNotFoundError(f"booking {booking_id} not found")

            conn.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            return self._row_to_booking(row)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Shared client
# ---------------------------------------------------------------------------

_client: CalendarClient | None = None


def get_calendar_client() -> CalendarClient:
    """The shared calendar client the tools use.

    One instance, so a test can configure a failure plan once and every tool
    call in that test sees it. The client is the mock here; Phase 6 swaps
    this function for a real provider without touching the tools.
    """
    global _client
    if _client is None:
        _client = MockCalendarClient()
    return _client


def set_calendar_client(client: CalendarClient | None) -> None:
    """Replace the shared client. Passing None resets it to a fresh mock.

    This is the seam a real provider is installed through.
    """
    global _client
    _client = client


def reset_calendar_client() -> None:
    """Drop the shared client so the next call builds a fresh mock."""
    set_calendar_client(None)


__all__ = [
    "CalendarError",
    "CalendarTimeoutError",
    "CalendarProviderError",
    "CalendarSlotUnavailableError",
    "CalendarNotFoundError",
    "CalendarClient",
    "MockCalendarClient",
    "get_calendar_client",
    "set_calendar_client",
    "reset_calendar_client",
]
