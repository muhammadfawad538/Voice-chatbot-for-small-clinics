-- Voice chatbot — v1 schema.
-- Single source of truth for the database. Executed by backend/app/db.py
-- with sqlite3.executescript(). Safe to re-run: every statement uses
-- IF NOT EXISTS.
--
-- PRAGMA foreign_keys is NOT set here. Foreign key enforcement is
-- per-connection, so db.connect() applies it to every new connection.
--
-- All timestamps are UTC TEXT in ISO-8601, written by the application.

-- ---------------------------------------------------------------------------
-- Schema lineage
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Calls
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_number_masked TEXT NOT NULL,
    status              TEXT NOT NULL
                        DEFAULT 'active'
                        CHECK (status IN ('active', 'completed', 'handoff', 'failed')),
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    retention_until     TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_status   ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_started  ON calls(started_at);

-- ---------------------------------------------------------------------------
-- Call turns — one utterance or reply
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS call_turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id    INTEGER NOT NULL REFERENCES calls(id),
    speaker    TEXT    NOT NULL CHECK (speaker IN ('caller', 'bot')),
    text       TEXT    NOT NULL,
    latency_ms INTEGER,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_call_turns_call ON call_turns(call_id);

-- ---------------------------------------------------------------------------
-- Tool calls
--
-- call_id is nullable: a tool can be exercised outside a phone call (tests,
-- staff trying the flow from the dashboard). A tool call with no call_id is
-- not part of a caller's transcript.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         INTEGER REFERENCES calls(id),
    tool_name       TEXT    NOT NULL,
    arguments_json  TEXT    NOT NULL DEFAULT '{}',
    result_json     TEXT,
    success         INTEGER NOT NULL DEFAULT 0 CHECK (success IN (0, 1)),
    latency_ms      INTEGER,
    injected        INTEGER NOT NULL DEFAULT 0 CHECK (injected IN (0, 1)),
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_call ON tool_calls(call_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name);

-- ---------------------------------------------------------------------------
-- Bookings
--
-- A slot holds at most one CONFIRMED booking. The partial unique index lets a
-- cancelled slot be booked again, which a plain UNIQUE(slot_start) would block.
-- Retries are safe: idempotency_key is UNIQUE and create_booking() returns the
-- original row when the key repeats.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bookings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_name          TEXT NOT NULL,
    caller_number_masked TEXT NOT NULL,
    slot_start           TEXT NOT NULL,
    slot_end             TEXT NOT NULL,
    status               TEXT NOT NULL
                         DEFAULT 'confirmed'
                         CHECK (status IN ('confirmed', 'cancelled')),
    idempotency_key      TEXT NOT NULL UNIQUE,
    created_at           TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_confirmed_slot
    ON bookings(slot_start)
    WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(slot_start);

-- ---------------------------------------------------------------------------
-- Handoffs — human escalation. The FK lives here: a handoff belongs to a call,
-- not the other way round.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS handoffs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      INTEGER REFERENCES calls(id),
    reason       TEXT    NOT NULL,
    status       TEXT    NOT NULL
                 DEFAULT 'pending'
                 CHECK (status IN ('pending', 'resolved', 'escalated')),
    created_at   TEXT    NOT NULL,
    resolved_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status);
CREATE INDEX IF NOT EXISTS idx_handoffs_call  ON handoffs(call_id);

-- ---------------------------------------------------------------------------
-- FAQ documents — knowledge base
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faq_documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    category   TEXT,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_faq_documents_active ON faq_documents(active);

-- ---------------------------------------------------------------------------
-- Retrievals — RAG lookup log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retrievals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id     INTEGER NOT NULL REFERENCES calls(id),
    query       TEXT    NOT NULL,
    document_id INTEGER NOT NULL REFERENCES faq_documents(id),
    score       REAL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retrievals_call ON retrievals(call_id);

-- ---------------------------------------------------------------------------
-- Evaluation results
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_name    TEXT NOT NULL,
    case_id      TEXT NOT NULL,
    passed       INTEGER NOT NULL CHECK (passed IN (0, 1)),
    details_json TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_results_name ON eval_results(eval_name);
