# Tech Stack

## Backend
- **Language:** Python 3.12
- **Web framework:** FastAPI (REST APIs for backend, mock calendar, business data)
- **Voice pipeline:** Pipecat (STT → LLM → TTS, plus Pipecat Flows for guided booking steps)
- **Validation:** Pydantic (typed contracts for tool arguments and bookings)

## Frontend
- **Framework:** React
- **Pages:** Call screen (voice interface), Staff dashboard (handoff review, call logs, FAQ management)

## Data & Storage
- **Relational DB:** SQLite — call logs, tool calls, handoff queue, mock calendar bookings, RAG retrieval logs, evaluation results
- **Vector DB:** Qdrant (local mode) or Chroma — FAQ knowledge base search, accessed through a `search_knowledge` interface
- **Upgrade path:** PostgreSQL + pgvector (documented, not part of v1)

## Key Design Decisions
- **No agent framework in v1.** The tool-calling loop is written by hand so we understand each step. LangGraph can be added later as an optional comparison.
- **Mock calendar first.** A FastAPI service backed by SQLite with `check_availability`, `book_slot`, `reschedule`, `cancel`. UNIQUE constraint on time slots prevents double bookings. Idempotency keys make retries safe. Failure injection switches for testing.
- **Text agent first, voice second.** Build and test the conversation logic as text, then add Pipecat voice pipeline.
- **Privacy:** Phone numbers masked in logs. Transcripts on a retention limit. Fake data only in testing.
