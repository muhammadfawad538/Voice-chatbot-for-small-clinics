# Roadmap

## Phase 1 — Project Skeleton
- [x] Set up project structure (backend, specs folders)
- [x] Create SQLite database schema (calls, call_turns, tool_calls, bookings, handoffs, FAQ docs, retrievals, eval_results)
- [x] Set up FastAPI app with health check endpoint

Spec: `specs/phase-01-backend-skeleton.md`.

## Phase 2a — Mock Calendar and Tools (no LLM)
- [x] Mock calendar API (check_availability, book_slot, reschedule, cancel)
- [x] Failure injection (timeout, server error, slot taken at last moment)
- [x] Tool functions with Pydantic input/output models and clean error results
- [x] Log every tool call (arguments, result, latency), phone numbers masked
- [x] Enforce confirmed=True for all write tools in code
- [x] handoff_to_human tool

## Phase 2b — Text Agent Loop
- [x] Conversation loop with Groq tool calling (receive → think → call tool → respond)
- [x] Validate tool arguments before running anything
- [x] Step limit, timeout and per-call cost tracking
- [x] Booking confirmation flow (read back name, date, time)
- [x] Handoff logic (complaints, urgent, medical, unsure → human)
- [x] Prompt injection tests for caller messages

## Phase 2c — FAQ Knowledge Base (RAG) — **SQLite FTS5 (BM25)**
- [x] 25 knowledge documents in `knowledge/` (Markdown with front matter, administrative topics only; planted-injection fixture moved to `tests/fixtures/`)
- [x] Indexer script `scripts/index_kb.py` → `faq_documents` + FTS5 virtual table; idempotent, `--rebuild`, deactivates removed docs on rebuild too
- [x] `KnowledgeSearch` interface + `FtsKnowledgeSearch` implementation (swappable for embeddings later)
- [x] `search_knowledge(query)` tool registered via `@logged_tool`, Pydantic I/O models, FTS5 query sanitization
- [x] Confidence threshold in config (`voicechat_kb_threshold`, tuned on dev set); below threshold → `found=false` → offer message
- [x] Every retrieval logged to `retrievals` table (query masked, document_id, score, rank)
- [x] Planted-instruction test: a document contains "Ignore previous instructions..." → does not cause a write through the loop (fixture isolated in `tests/fixtures/`)
- [x] `kb_mode` setting (`none` | `full_prompt` | `rag`); system prompt template selects behavior
- [x] Offline eval `scripts/eval_kb.py`: hit rate @1/@3 by type on held-out set, threshold tuned on dev set (2.6), writes to `eval_results`
- [x] Evaluation set `eval/questions.csv`: 40 questions (21 dev, 19 held-out); paraphrase questions audited and relabelled (q16 dev, q34+q35 heldout relabelled normal)
- [x] Live eval `scripts/eval_kb_live.py`: three `kb_mode`s on held-out, key-fact correctness, correct refusal, confident-but-wrong, avg + p95 latency, token usage (requires `GROQ_API_KEY`)
- [ ] Compare: no KB vs full FAQ in prompt vs RAG (correctness, latency) — **not yet measured**

**Held-out retrieval results** (threshold 2.6, tuned on dev): hit@1 0.500 (7/14), hit@3 0.643 (9/14), correct_refusal 1.000 (5/5).

**Planted-instruction test**: ran live injection tests once with Groq — 15 passed, 0 failed, all 7 live tests passed.

**Comparison pending**: needs one valid run per `kb_mode` (rate-limited free tier). The previous attempt was invalidated by the Groq daily token limit, which scored 429 failures as wrong answers; all three modes must be re-run. See "How to resume the three-way comparison" in `specs/phase-02c-rag.md`.

**Model note:** the default Groq model was `llama-3.3-70b-versatile`, which Groq has retired (404). The default is now `openai/gpt-oss-120b` — the only tool-calling model on the free tier. A model that does not call tools produces an agent that silently never reaches the knowledge base, and every retrieval metric reads as zero.

## Phase 3 — Voice Pipeline (browser only)
- [ ] Feasibility check: record PC specs, confirm pipecat-ai installs/imports on Python 3.14; write specs/phase-03-voice-spike.md with the lightest working stack plus a fallback (research only, no product code) — **DONE: decision = Web Speech API, no Pipecat**
- [ ] Browser call screen: plain HTML/JS page served by FastAPI (no React, no build step)
- [ ] Browser speech APIs: SpeechRecognition (STT), SpeechSynthesis (TTS); AI disclosure spoken at start
- [ ] Barge-in: cancel speechSynthesis when user starts speaking (JS-side)
- [ ] POST /api/v1/voice/turn endpoint: transcript + session_id → agent loop → reply text + per-stage timings; audio never reaches server
- [ ] Echo test mode: page speaks back what it heard without calling agent, to prove audio path
- [ ] Latency measured in browser per stage (recognition, synthesis, network RTT); sent to server for logging
- [ ] Later: comparison against hosted speech-to-text fallback (optional)

## Phase 4 — Staff Dashboard
- [ ] Call log viewer (transcripts, masked phone numbers)
- [ ] Handoff queue (review, resolve, escalate)
- [ ] FAQ management (add/edit documents, re-index)
- [ ] Analytics (calls per day, booking success rate, handoff rate)

## Phase 5 — Evaluation and Hardening
- [ ] Unit and integration tests for the full booking flow
- [ ] Failure injection tests (calendar timeout, slot taken, STT garbage, tool errors)
- [ ] Scripted test calls (~50): accents, noise, interruptions, changed minds, wrong details, double-booking attempts, urgent requests, injection attempts
- [ ] RAG evaluation (~40 questions incl. unanswerable) with hit rate and confident-but-wrong count
- [ ] Report wrong bookings and wrong handoffs separately, plus latency and cost per call
- [ ] PII retention cleanup job (delete expired transcripts)
- [ ] (Optional) Replay failed calls from logs after a prompt or model change
- [ ] (Optional) Basic concurrency check, with limits documented honestly

## Phase 6 — Real Calendar Integration
- [ ] Connect Cal.com or Google Calendar API behind the existing interface
- [ ] Replace mock calendar without changing agent code

## Phase 7 — Launch
- [ ] README: problem, flow diagram, stack table with reasons, results table, "what broke and how I fixed it"
- [ ] Demo video of a browser voice call (book, interrupt, correct a detail, handoff)
- [ ] LinkedIn post with real numbers, labeled as test data

## Out of scope (deliberate)
- Phone/Twilio integration: the Pipecat transport can be swapped later; the demo runs in the browser
- Real customer data: all data is fake
