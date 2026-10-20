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

## Phase 2c — FAQ Knowledge Base (RAG)
- [ ] Write fake knowledge base documents (20 to 40, short, with updated dates)
- [ ] Index into the local vector store (Qdrant) and build the search_knowledge tool
- [ ] Confidence threshold: "no confident match" → take a message
- [ ] Log every retrieval (query, chunks, scores)
- [ ] Planted-instruction test in a knowledge document
- [ ] Compare: no KB vs full FAQ in prompt vs RAG (correctness, latency)

## Phase 3 — Voice Pipeline (browser only)
- [ ] Reconcile Python version (3.12 vs installed 3.14) and verify speech packages install
- [ ] Integrate Pipecat for STT → agent → TTS (local Whisper, Piper, Silero VAD first)
- [ ] Use Pipecat Flows for guided booking steps
- [ ] Browser call screen over WebRTC (start from Pipecat's starter client; build React only if needed)
- [ ] AI disclosure at the start of every call
- [ ] Interruption handling and read-back confirmation for names, numbers and dates
- [ ] Measure latency per turn; compare local vs hosted speech if needed

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
