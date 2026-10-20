# Phase 1 — Backend Skeleton

**Status:** Complete (backend only)
**Depends on:** none
**Supersedes:** nothing
**Source:** `specs/roadmap.md` Phase 1

## Delivered

- Repository layout, `pyproject.toml` + `requirements.txt`, `.gitignore`, `.env.example`, `README.md`.
- `schema.sql` — nine tables, executed by `sqlite3.executescript`.
- `backend/app/db.py` — per-connection PRAGMAs, `init_db()`, `create_booking()`.
- `backend/app/{config,main}.py`, `routers/health.py`, `models/health.py`.
- `scripts/init_db.py` — `--reset`, idempotent, works as script and module.
- `tests/` — 15 tests over the database invariants and the health contract.

Verified: `pytest` green, `ruff` clean, `GET /api/v1/health` returns `200` from a
real `uvicorn` process.

## Purpose

Stand up the backend foundation for the voice AI receptionist: a FastAPI service, a
SQLite database with the full v1 schema, and a health check that proves both are
alive. Everything in Phase 2 (agent loop, tools, calendar, RAG) is built on this.

This phase is backend-only. The React call screen is deferred to a follow-up step.

## Scope

### In scope

- Repository layout, dependency manifests, `.gitignore`, `.env.example`.
- `schema.sql` — the single source of truth for the v1 database schema.
- `scripts/init_db.py` — builds the database from `schema.sql`.
- FastAPI app with `GET /health`, mounted under `/api/v1`.
- First unit tests for the database invariants and the health response.

### Out of scope

- React frontend and the call screen.
- The conversation loop, tool calling, and Pydantic tool schemas.
- Pipecat, STT/TTS, WebRTC.
- Staff dashboard.
- Any migration tooling. The dev database holds fake data only and can be dropped
  and rebuilt freely, so schema changes are applied by editing `schema.sql` and
  re-running `init_db.py --reset`. A migration tool is added only if the schema
  needs to change after real data exists.

## Data contract

`schema.sql` defines nine tables. All timestamps are UTC `TEXT` in ISO-8601.
`PRAGMA foreign_keys` is **not** relied on from `schema.sql` — it is per-connection
and is applied by `db.connect()` on every new connection.

| table | Purpose | Key columns |
|---|---|---|
| `schema_version` | Schema lineage | `version INTEGER PK`, `applied_at` |
| `calls` | One phone call | `id PK`, `caller_number_masked`, `status`, `started_at`, `ended_at`, `retention_until` |
| `call_turns` | One utterance or reply | `id PK`, `call_id FK→calls`, `speaker`, `text`, `latency_ms`, `created_at` |
| `tool_calls` | One tool invocation | `id PK`, `call_id FK→calls`, `tool_name`, `arguments_json`, `result_json`, `success`, `latency_ms`, `created_at` |
| `bookings` | Calendar reservation | `id PK`, `caller_name`, `caller_number_masked`, `slot_start`, `slot_end`, `status`, `idempotency_key`, `created_at` |
| `handoffs` | Human escalation | `id PK`, `call_id FK→calls`, `reason`, `status`, `created_at`, `resolved_at` |
| `faq_documents` | Knowledge base entries | `id PK`, `question`, `answer`, `category`, `active`, `created_at` |
| `retrievals` | RAG lookup log | `id PK`, `call_id FK→calls`, `query`, `document_id FK→faq_documents`, `score`, `created_at` |
| `eval_results` | Evaluation runs | `id PK`, `eval_name`, `case_id`, `passed`, `details_json`, `created_at` |

### Invariants

1. **A slot holds at most one confirmed booking.** Enforced by a partial unique
   index on `bookings(slot_start) WHERE status = 'confirmed'`, so a cancelled slot
   can be booked again.
2. **Retries are safe.** `bookings.idempotency_key` is `UNIQUE`. `create_booking()`
   returns the existing booking when the key repeats instead of raising.
3. **Foreign keys are enforced.** `PRAGMA foreign_keys = ON` on every connection.
   A `tool_calls` row with a nonexistent `call_id` is rejected.
4. **`init_db()` is idempotent.** `schema_version` is stamped with
   `INSERT OR IGNORE`; running it twice never fails or duplicates the row.

### CHECK constraints

- `calls.status` — `active`, `completed`, `handoff`, `failed`
- `bookings.status` — `confirmed`, `cancelled`
- `handoffs.status` — `pending`, `resolved`, `escalated`

### Known limitation

`caller_number_masked` stores a masked number such as `(XXX) XXX-1234`. A masked
number cannot receive an SMS, so **booking confirmations by SMS are not possible
in v1**. A secure or hashed lookup is required before confirmation messages can
be sent. Flagged for Phase 2.

## API contract

### `GET /api/v1/health`

Success — `200`:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "database": "ok",
  "timestamp": "2026-10-19T12:00:00+00:00"
}
```

The handler verifies the database by checking the expected table set **exactly**.
Any missing table or connection failure yields `503` with `"database": "error"`.
**Raw exception text is never returned in the response body.**

### `GET /`

Service name and links to `/api/v1/health` and `/docs`.

## Configuration

`backend/app/config.py` is the single home for settings, loaded from the
environment by pydantic-settings:

| variable | default | meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/voicechatbot.db` | SQLite path (SQLAlchemy-style URL, `sqlite:///` prefix stripped) |
| `APP_NAME` | `voice-chatbot` | service name |
| `ENVIRONMENT` | `development` | `development` / `production` |
| `CORS_ORIGINS` | `http://localhost:3000` | comma-separated allow-list |

`.env` is gitignored. `.env.example` contains `DATABASE_URL` only — no keys.

## Database lifecycle

Development data is fake and disposable. To rebuild from scratch:

```
python scripts/init_db.py --reset
```

`init_db.py` also creates the `data/` folder when it does not exist, and works
both as `python scripts/init_db.py` and `python -m scripts.init_db` from the
repo root.

## Acceptance criteria

- [x] Repository structure, dependency manifests, `.gitignore`, `.env.example` exist.
- [x] `schema.sql` defines all nine tables with the invariants above.
- [x] `init_db()` creates the database, creates `data/` if missing, is idempotent.
- [x] Foreign keys are enforced per connection.
- [x] A cancelled slot can be booked again; two confirmed bookings on the same slot cannot exist.
- [x] `create_booking()` is idempotent on `idempotency_key`.
- [x] `GET /api/v1/health` returns the contract shape, and `503` without the DB.
- [x] Tests cover each of the above and pass with `pytest -q` (15 tests).
- [x] `uvicorn` serves the app and `/health` returns `200`.

## Deferred from roadmap Phase 1

- React app with basic call screen placeholder — the browser call screen now lives
  in Phase 3 of `specs/roadmap.md`.
