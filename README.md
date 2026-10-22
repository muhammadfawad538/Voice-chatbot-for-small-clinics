# Voice chatbot

A voice AI receptionist for small businesses (clinics, salons, restaurants). It
answers every call, books appointments through a calendar API, answers FAQs from
the business's knowledge base, and hands off to a human for anything complex.

Specs in [`specs/`](specs/) are the source of truth. `specs/roadmap.md` is the
plan; each phase has its own feature spec.

## Status

Phase 2c — FAQ Knowledge Base complete. See
[`specs/phase-02c-rag.md`](specs/phase-02c-rag.md).

Phase 2b — Text Agent Loop. See
[`specs/phase-02b-text-agent-loop.md`](specs/phase-02b-text-agent-loop.md).

- Agent loop with Groq tool calling (`openai/gpt-oss-120b`)
- Fake LLM for unit tests (no API key needed in CI)
- Step limit, timeout, cost tracking
- Booking confirmation flow with `confirmed=True` tripwire
- Six handoff reasons via `handoff_to_human`
- Prompt-injection test suite (code-enforced + live model)
- FAQ knowledge base over SQLite FTS5 (BM25), no new dependencies
- `search_knowledge` tool, `retrievals` audit table, `kb_mode` setting
- Offline eval (`scripts/eval_kb.py`) + optional live eval (`scripts/eval_kb_live.py`)

Next: Phase 3 — Voice Pipeline (browser only).

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
                  app/knowledge/    KnowledgeSearch interface + FTS5 backend
                  app/agent/        Loop, Groq client, system prompt
                  app/tools/        Calendar tools, search_knowledge, decorator
scripts/          Operational scripts (init_db, index_kb, eval_kb, eval_kb_live)
tests/            pytest suite
specs/            Feature specs and roadmap
knowledge/        FAQ documents (Markdown + front matter)
eval/             Question set for retrieval evaluation
schema.sql        Database schema
```

## Knowledge base

The FAQ index lives in the same SQLite database as everything else — an FTS5
virtual table (`faq_fts`) over `faq_documents`, kept in sync by triggers. No
vector store, no embeddings, no new dependencies.

```
python scripts/index_kb.py            # incremental update
python scripts/index_kb.py --rebuild  # drop and recreate the FTS index
python scripts/eval_kb.py             # offline eval: hit@1/@3, threshold tuning
```

`VOICECHAT_KB_MODE` selects behaviour: `none`, `full_prompt` (the FAQ stays
hardcoded in the system prompt), or `rag` (the agent calls `search_knowledge`
and answers only from the returned text).
