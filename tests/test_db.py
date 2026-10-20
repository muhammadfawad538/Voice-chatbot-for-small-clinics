"""Database invariants: schema, foreign keys, slot uniqueness, idempotency."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import (
    SCHEMA_VERSION,
    BookingConflictError,
    connect,
    create_booking,
    init_db,
    list_tables,
)

SLOT = "2026-11-02T10:00:00+00:00"
SLOT_END = "2026-11-02T10:30:00+00:00"


def _insert_call(conn: sqlite3.Connection, masked: str = "(XXX) XXX-1234") -> int:
    """Helper: insert a call, returning its id."""
    cur = conn.execute(
        "INSERT INTO calls (caller_number_masked, status, started_at) "
        "VALUES (?, 'active', '2026-10-19T12:00:00+00:00')",
        (masked,),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_db_initializes_from_schema(db_path, expected_tables):
    tables = set(list_tables(connect(db_path)))
    assert tables == expected_tables


def test_init_db_creates_missing_parent_folder(tmp_path):
    path = tmp_path / "nested" / "deep" / "test.db"
    init_db(path)
    assert path.exists()


def test_init_db_is_idempotent(db_path):
    """Running twice neither fails nor duplicates schema_version."""
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        tables = set(list_tables(conn))
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["version"] == SCHEMA_VERSION
    assert "calls" in tables


# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------


def test_foreign_keys_enforced(db_path):
    """A tool_calls row with a nonexistent call_id is rejected."""
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO tool_calls
                    (call_id, tool_name, arguments_json, success, created_at)
                VALUES (9999, 'check_availability', '{}', 1, '2026-10-19T12:00:00+00:00')
                """
            )
        conn.rollback()
    finally:
        conn.close()


def test_foreign_keys_reject_handoff_without_call(db_path):
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO handoffs (call_id, reason, status, created_at)
                VALUES (9999, 'complaint', 'pending', '2026-10-19T12:00:00+00:00')
                """
            )
        conn.rollback()
    finally:
        conn.close()


def test_status_check_constraints(db_path):
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO calls (caller_number_masked, status, started_at) "
                "VALUES ('(XXX) XXX-1234', 'nonsense', '2026-10-19T12:00:00+00:00')"
            )
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Slot uniqueness
# ---------------------------------------------------------------------------


def test_two_confirmed_bookings_same_slot_rejected(db_path):
    conn = connect(db_path)
    try:
        create_booking(
            conn,
            caller_name="Ada",
            caller_number_masked="(XXX) XXX-1234",
            slot_start=SLOT,
            slot_end=SLOT_END,
            idempotency_key="key-a",
        )
        with pytest.raises(BookingConflictError):
            create_booking(
                conn,
                caller_name="Bob",
                caller_number_masked="(XXX) XXX-5678",
                slot_start=SLOT,
                slot_end=SLOT_END,
                idempotency_key="key-b",
            )
        conn.rollback()

        count = conn.execute("SELECT count(*) FROM bookings").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_cancelled_slot_can_be_rebooked(db_path):
    conn = connect(db_path)
    try:
        first = create_booking(
            conn,
            caller_name="Ada",
            caller_number_masked="(XXX) XXX-1234",
            slot_start=SLOT,
            slot_end=SLOT_END,
            idempotency_key="key-a",
        )
        conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (first["id"],))
        conn.commit()

        second = create_booking(
            conn,
            caller_name="Bob",
            caller_number_masked="(XXX) XXX-5678",
            slot_start=SLOT,
            slot_end=SLOT_END,
            idempotency_key="key-b",
        )
        assert second["id"] != first["id"]
        assert second["status"] == "confirmed"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_booking_returns_original(db_path):
    conn = connect(db_path)
    try:
        first = create_booking(
            conn,
            caller_name="Ada",
            caller_number_masked="(XXX) XXX-1234",
            slot_start=SLOT,
            slot_end=SLOT_END,
            idempotency_key="retry-key",
        )
        second = create_booking(
            conn,
            caller_name="Ada",
            caller_number_masked="(XXX) XXX-1234",
            slot_start=SLOT,
            slot_end=SLOT_END,
            idempotency_key="retry-key",
        )

        assert first["id"] == second["id"]
        count = conn.execute("SELECT count(*) FROM bookings").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_duplicate_idempotency_key_is_unique_at_the_db_level(db_path):
    conn = connect(db_path)
    try:
        create_booking(
            conn,
            caller_name="Ada",
            caller_number_masked="(XXX) XXX-1234",
            slot_start=SLOT,
            slot_end=SLOT_END,
            idempotency_key="same-key",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO bookings
                    (caller_name, caller_number_masked, slot_start, slot_end,
                     status, idempotency_key, created_at)
                VALUES ('Bob', '(XXX) XXX-5678', '2026-11-03T10:00:00+00:00',
                        '2026-11-03T10:30:00+00:00', 'confirmed', 'same-key',
                        '2026-10-19T12:00:00+00:00')
                """
            )
        conn.rollback()
    finally:
        conn.close()
