"""Database access.

sqlite3, no ORM. schema.sql is the source of truth for the layout; this module
owns connections, initialisation, and the small amount of write logic that has
to be atomic (create_booking).

Foreign key enforcement is per-connection in SQLite, so PRAGMA foreign_keys is
applied here on every new connection rather than in schema.sql.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.config import SCHEMA_PATH, get_settings

SCHEMA_VERSION = 2

# Applied by /health. An exact match is required: a missing table means the
# schema did not initialise, an extra one means it drifted from schema.sql.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "schema_version",
        "calls",
        "call_turns",
        "tool_calls",
        "bookings",
        "handoffs",
        "faq_documents",
        "retrievals",
        "eval_results",
    }
)

# Applied on every new connection. journal_mode and busy_timeout are database-
# level and persistent, but foreign_keys is not — it must be set per connection.
PRAGMAS: tuple[str, ...] = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
)


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the required PRAGMAs applied.

    PRAGMA foreign_keys is per-connection, so it cannot live in schema.sql.
    """
    path = Path(db_path) if db_path is not None else get_settings().db_path
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


def init_db(db_path: Path | str | None = None) -> list[str]:
    """Build the database from schema.sql. Idempotent.

    Creates the parent folder if needed, applies the schema, and stamps
    schema_version with INSERT OR IGNORE so a repeat run is a no-op.
    Returns the resulting table names.
    """
    path = Path(db_path) if db_path is not None else get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(path)
    try:
        conn.executescript(schema)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )
        conn.commit()
        return list_tables(conn)
    finally:
        conn.close()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Names of all user tables, sorted."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def check_schema(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """Return (healthy, problems). Healthy means the table set matches exactly."""
    problems: list[str] = []
    try:
        present = set(list_tables(conn))
    except sqlite3.Error:
        return False, ["database unavailable"]

    missing = sorted(EXPECTED_TABLES - present)
    extra = sorted(present - EXPECTED_TABLES)
    if missing:
        problems.append(f"missing tables: {', '.join(missing)}")
    if extra:
        problems.append(f"unexpected tables: {', '.join(extra)}")
    return not problems, problems


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


class BookingConflictError(Exception):
    """A confirmed booking already holds the requested slot."""


def create_booking(
    conn: sqlite3.Connection | None = None,
    *,
    caller_name: str,
    caller_number_masked: str,
    slot_start: str,
    slot_end: str,
    idempotency_key: str,
    status: str = "confirmed",
) -> sqlite3.Row:
    """Create a booking, or return the original if the key repeats.

    Idempotency: the same idempotency_key always yields the same booking, so a
    retried request never double-books. A different key targeting a slot that a
    confirmed booking already holds raises BookingConflictError.

    A repeat whose existing row is *cancelled* revives it — status back to
    confirmed — rather than returning it as-is. The key is derived from the
    caller and the slot, so a repeat with a cancelled row means the caller
    booked that slot, cancelled it, and wants it again.

    Pass ``conn=None`` to let the function open and close its own connection.
    """
    owned = conn is None
    if conn is None:
        conn = connect()
    try:
        existing = conn.execute(
            "SELECT * FROM bookings WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            if existing["status"] == "cancelled" and status == "confirmed":
                conn.execute(
                    "UPDATE bookings SET status = 'confirmed' WHERE id = ?",
                    (existing["id"],),
                )
                conn.commit()
                return get_booking(conn, existing["id"])
            return existing

        try:
            cursor = conn.execute(
                """
                INSERT INTO bookings
                    (caller_name, caller_number_masked, slot_start, slot_end,
                     status, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    caller_name,
                    caller_number_masked,
                    slot_start,
                    slot_end,
                    status,
                    idempotency_key,
                    utcnow(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "ux_bookings_confirmed_slot" in str(exc) or "slot_start" in str(exc):
                raise BookingConflictError(
                    f"slot {slot_start} is already confirmed by another booking"
                ) from exc
            raise
        return get_booking(conn, cursor.lastrowid)
    finally:
        if owned:
            conn.close()


def get_booking(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def log_tool_call(
    conn: sqlite3.Connection,
    *,
    call_id: int | None,
    tool_name: str,
    arguments_json: str,
    result_json: str | None,
    success: bool,
    latency_ms: int,
    injected: bool = False,
) -> int:
    """Write one tool_calls row. Returns the new row id.

    ``injected`` marks a failure the test suite asked for, so evaluation can
    tell an injected failure from a real bug.
    """
    cursor = conn.execute(
        """
        INSERT INTO tool_calls
            (call_id, tool_name, arguments_json, result_json, success,
             latency_ms, injected, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call_id,
            tool_name,
            arguments_json,
            result_json,
            1 if success else 0,
            latency_ms,
            1 if injected else 0,
            utcnow(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def cancel_booking(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row:
    """Cancel a booking, releasing its slot for rebooking."""
    conn.execute(
        "UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,)
    )
    conn.commit()
    return get_booking(conn, booking_id)
