# Voice chatbot

A voice AI receptionist for small businesses (clinics, salons, restaurants). It
answers every call, books appointments through a calendar API, answers FAQs from
the business's knowledge base, and hands off to a human for anything complex.

Specs in [`specs/`](specs/) are the source of truth. `specs/roadmap.md` is the
plan; each phase has its own feature spec.

## Status

Phase 2b — Text Agent Loop complete. See
[`specs/phase-02b-text-agent-loop.md`](specs/phase-02b-text-agent-loop.md).

- Agent loop with Groq tool calling (`llama-3.3-70b-versatile`)
- Fake LLM for unit tests (no API key needed in CI)
- Step limit, timeout, cost tracking
- Booking confirmation flow with `confirmed=True` tripwire
- Six handoff reasons via `handoff_to_human`
- Prompt-injection test suite (code-enforced + live model)

Next: Phase 2c — FAQ Knowledge Base (RAG).

## Requirements

- Python 3.12+
- Node.js 18+ (only once the React call screen lands in a later phase)

## Setup

```
python -m venv .venv
source .venv/Scripts/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env                 # optional; defaults work out of the box
```

## Running

```
uvicorn app.main:app --app-dir backend --port 8000
```

- `GET /` — service info
- `GET /api/v1/health` — service and database status
- `/docs` — OpenAPI UI

## Database

`schema.sql` is the single source of truth for the layout. The database is
disposable development data and can be rebuilt at any time:

```
python scripts/init_db.py --reset
```

`PRAGMA foreign_keys` is applied per connection by `backend/app/db.py`, not by
`schema.sql` — SQLite only enforces foreign keys on connections that ask for it.

No migration tooling in v1; the schema is edited and the database recreated.
A migration tool is added only if the schema needs to change after real data
exists.

## Tests

```
pytest
```

## Layout

```
backend/app/      FastAPI app, config, db access, routers, models
scripts/          Operational scripts (init_db)
tests/            pytest suite
specs/            Feature specs and roadmap
schema.sql        Database schema
```
