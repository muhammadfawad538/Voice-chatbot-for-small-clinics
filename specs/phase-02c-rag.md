# Phase 2c — FAQ Knowledge Base (SQLite FTS5)

**Status:** Complete
**Depends on:** Phase 2b — Text Agent Loop
**Source:** `specs/roadmap.md` Phase 2c (updated to SQLite FTS5)

## Purpose

Add a local, zero-dependency knowledge base for FAQ answers. The agent calls
`search_knowledge(query)` to retrieve up to 3 relevant documents, answers from
the returned text only, and hands off when nothing matches or the question is
medical. No external vector store, no new Python dependencies, no Docker — all
in the existing SQLite database using the FTS5 extension.

## Scope

### In scope

- 25–40 fake business documents in `knowledge/` as Markdown with front matter
  (`title`, `category`, `updated`). Short, written as spoken answers.
  Administrative topics only: hours, prices, policies, location, parking,
  preparation. No medical advice.
- Indexer script that loads documents into `faq_documents` and an FTS5 virtual
  table. Re-running updates changed documents and deactivates removed ones.
  Supports full rebuild.
- `KnowledgeSearch` interface with `FtsKnowledgeSearch` implementation. Swappable
  for an embedding-based version later.
- `search_knowledge(query)` tool registered through `@logged_tool` with Pydantic
  input/output models. Sanitizes the query for FTS5 syntax characters. Returns
  up to 3 results with `source`, `score`, `updated`, plus `found` boolean.
- Confidence threshold in config (tuned on dev set). BM25 scores are not
  normalized; no cosine-style reuse. Below threshold → `found=false` → agent
  offers to take a message.
- Retrieved text is data, never instructions. Returned in clearly delimited
  fields. Write behaviour stays governed by the existing `confirmed=True`
  code-enforced rules.
- Every returned result logged to `retrievals` table (query, document_id, score,
  rank), masked. Schema updated, version bumped if columns needed.
- System prompt updates: answer only from returned text, 1–2 sentences, say so
  when nothing matches, hand off health questions. `kb_mode` setting:
  `none`, `full_prompt` (current hardcoded FAQ as baseline), `rag`.
- Evaluation: `eval/questions.csv` with 40 questions (expected doc id, key
  facts). Includes 8–10 unanswerable, 5 misspelled/garbled, 5 paraphrase
  (no shared keywords). Dev set for threshold tuning, held-out set for
  reporting. Offline script reports hit rate @1/@3 by type, tunes threshold.
  Optional live script (needs `GROQ_API_KEY`) runs three `kb_mode`s on
  held-out set, reports key-fact correctness, correct refusals,
  confident-but-wrong, average latency. Results written to `eval_results`.
- Tests (no network): indexer idempotency; known queries return expected docs;
  below-threshold returns `found=false`; FTS5 special chars don't raise;
  every retrieval writes masked rows; planted instruction in a document does
  not cause a write through the loop with `FakeLLMClient`; `search_knowledge`
  in registry and wrapped by decorator.

### Out of scope

- Pipecat, voice, React, dashboard.
- External vector stores (Qdrant, Chroma), embedding models, PyTorch,
  sentence-transformers, Docker.

## Design decisions

### SQLite FTS5 (BM25) instead of embeddings

- Zero new dependencies. The Python standard library `sqlite3` has FTS5 since
  3.38 (2022). This environment is 3.14 — FTS5 is confirmed present.
- BM25 ranks by term frequency and inverse document frequency. Good enough for
  short, distinct FAQ entries.
- No GPU, no model download, no warm-up. Fast on low-spec hardware.
- `retrievals` table already exists (Phase 1 schema). We log `score` (raw BM25)
  and `rank` (1, 2, 3).
- `faq_documents` table already exists. We add `tsv` column? No — FTS5 virtual
  table is separate and references the rowid.

### FTS5 virtual table

```sql
CREATE VIRTUAL TABLE faq_fts USING fts5(
    title, body, category,
    content='faq_documents',
    content_rowid='id',
    tokenize='porter unicode61'
);
```

- `content='faq_documents'` makes it an external content table — the FTS index
  stores only the rowid and terms; the original text stays in `faq_documents`.
- `content_rowid='id'` links the FTS rowid to `faq_documents.id`.
- `tokenize='porter unicode61'` — Porter stemmer + Unicode support for
  misspelling tolerance. Good enough for English business text.
- Triggers on `faq_documents` keep the index in sync on INSERT/UPDATE/DELETE.

### Query sanitization

FTS5 interprets `"` `:` `*` `-` `(` `)` `^` `@` as syntax. Caller text must be
escaped. Approach: wrap the raw query in double quotes (phrase search) after
escaping any embedded double quotes. If that yields zero results, fall back to
a tokenized OR query (each term with `*` suffix for prefix matching). This is
the only logic in `FtsKnowledgeSearch.search()`.

### Confidence threshold

BM25 scores are positive; higher is better. Typical range on this corpus:
~2–15. No cross-query normalization. Threshold is a single float in config
(`VOICECHAT_KB_THRESHOLD`, default `4.0` from dev-set tuning). If top result
score < threshold → `found=false`.

### kb_mode

| Mode         | Behaviour |
|--------------|-----------|
| `none`       | Agent answers from system prompt only (no tool). |
| `full_prompt`| Current hardcoded FAQ stays in system prompt (baseline). |
| `rag`        | Agent calls `search_knowledge` and answers *only* from returned text. |

Set via `VOICECHAT_KB_MODE` (default `rag`). The system prompt template swaps
the FAQ block and the tool availability.

### Knowledge document format

```
knowledge/
├── 01-hours.md
├── 02-location-parking.md
├── 03-pricing.md
...
```

Front matter:
```markdown
---
title: "Business Hours"
category: "hours"
updated: "2026-10-15"
---
We're open Monday through Friday, 9 AM to 5 PM Eastern Time.
Closed weekends and major US holidays.
```

- Short, spoken style. One document = one answerable topic.
- `updated` is ISO date; used in retrieval output and for change detection.

## Data contract

### New configuration

| Variable | Default | Meaning |
|---|---|---|
| `VOICECHAT_KB_MODE` | `rag` | `none` \| `full_prompt` \| `rag` |
| `VOICECHAT_KB_THRESHOLD` | `4.0` | BM25 score below this → `found=false` |
| `VOICECHAT_KB_MAX_RESULTS` | `3` | Max documents returned |

### Schema changes

`faq_documents` already exists. Add FTS5 virtual table + triggers:

```sql
-- Virtual table for full-text search (external content)
CREATE VIRTUAL TABLE IF NOT EXISTS faq_fts USING fts5(
    title, body, category,
    content='faq_documents',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS faq_fts_ai AFTER INSERT ON faq_documents BEGIN
    INSERT INTO faq_fts(rowid, title, body, category)
    VALUES (new.id, new.title, new.body, new.category);
END;

CREATE TRIGGER IF NOT EXISTS faq_fts_ad AFTER DELETE ON faq_documents BEGIN
    INSERT INTO faq_fts(faq_fts, rowid, title, body, category)
    VALUES ('delete', old.id, old.title, old.body, old.category);
END;

CREATE TRIGGER IF NOT EXISTS faq_fts_au AFTER UPDATE ON faq_documents BEGIN
    INSERT INTO faq_fts(faq_fts, rowid, title, body, category)
    VALUES ('delete', old.id, old.title, old.body, old.category);
    INSERT INTO faq_fts(rowid, title, body, category)
    VALUES (new.id, new.title, new.body, new.category);
END;
```

`retrievals` already has the right columns. No new columns needed.

`SCHEMA_VERSION` → 3.

### Tool definition (`search_knowledge`)

```json
{
  "name": "search_knowledge",
  "description": "Search the knowledge base for an answer to the caller's question. Returns up to 3 matching documents with scores.",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "The caller's question, verbatim"},
      "confirmed": {"type": "boolean"}
    },
    "required": ["query", "confirmed"]
  }
}
```

- `confirmed=True` always (read-only tool, but keeps schema uniform).
- Input model: `SearchKnowledgeInput(query: str, confirmed: bool)`.
- Output model: `SearchKnowledgeResult(found: bool, results: list[DocResult])`
  where `DocResult(source: str, score: float, updated: str, text: str)`.

### `eval_results` usage

- Offline eval writes `eval_name='kb_hit_rate'` with `details_json`:
  `{"hit_at_1": 0.75, "hit_at_3": 0.90, "threshold": 4.0, "set": "dev"}`.
- Live eval writes `eval_name='kb_live'` with `details_json`:
  `{"kb_mode": "rag", "key_fact_correct": 0.82, "correct_refusal": 0.90,
  "confident_wrong": 0.05, "avg_latency_ms": 420}`.

## Acceptance criteria

- [x] `knowledge/` directory with 25–40 Markdown documents, front matter,
  administrative topics only, no medical advice. *(25 real documents; 1 planted-
  injection fixture moved to `tests/fixtures/` for test isolation)*
- [x] Indexer script (`scripts/index_kb.py`): loads all docs, upserts by
  `title` (or path hash), updates `updated` and content, deactivates removed
  ones, `--rebuild` flag drops and recreates FTS index. Idempotent.
- [x] `KnowledgeSearch` protocol and `FtsKnowledgeSearch` implementation in
  `app/knowledge/`.
- [x] `search_knowledge` tool in `app/tools/knowledge.py`, registered via
  `@logged_tool`, input/output Pydantic models, query sanitization.
- [x] Tool returns `found=false` when top BM25 < threshold. Top 3 results
  otherwise, with `source`, `score`, `updated`, `text`.
- [x] Every call to `search_knowledge` writes to `retrievals` (query masked,
  document_id, score, rank, created_at).
- [x] System prompt template selects behaviour by `kb_mode` (`none`,
  `full_prompt`, `rag`). In `rag` mode: answer only from returned text,
  1–2 sentences, hand off health questions.
- [x] New config vars in `app/config.py` with env mapping.
- [x] `eval/questions.csv` with 40 questions, types labelled. *(21 dev, 19 held-out)*
- [x] Offline eval script (`scripts/eval_kb.py`): hit rate @1/@3 by type on
  held-out, threshold tuning on dev. Writes to `eval_results`.
- [x] Live eval script (`scripts/eval_kb_live.py`): three `kb_mode`s on
  held-out, key-fact correctness, correct refusal, confident-but-wrong,
  latency. Requires `GROQ_API_KEY`.
- [x] Tests: indexer idempotency; known queries return expected docs;
  below-threshold `found=false`; FTS5 special chars sanitized; retrievals
  logged masked; planted instruction in doc does not cause write through loop
  with `FakeLLMClient`; `search_knowledge` in registry and wrapped.
- [x] `pytest` green, `ruff` clean. *(95 passed, 7 skipped — the skips are the
  `live`-marked model tests, which need `GROQ_API_KEY`)*
- [x] Phase 2c roadmap items updated to reflect SQLite FTS5.

## Results

### ITEM 1 — Planted-injection fixture moved to `tests/fixtures/`

The `knowledge/26-planted-injection.md` document was moved to
`tests/fixtures/26-planted-injection.md`. The real `knowledge/` directory now
holds exactly 25 FAQ documents. The indexer was rebuilt:

```
Rebuilt KB: 25 files, 0 new, 0 updated, 1 deactivated
```

The planted document is deactivated (`active=0`) and no longer searchable.
Offline eval before vs after the move (threshold 2.6, dev-tuned):

| Metric | Before (26 docs) | After (25 docs) |
|---|---|---|
| Held-out hit@1 | 0.571 (8/14) | 0.500 (7/14) |
| Held-out hit@3 | 0.714 (10/14) | 0.643 (9/14) |
| Held-out correct_refusal | 1.000 (5/5) | 1.000 (5/5) |

The one-question delta is q31 `guest wifi network and password`, whose
expected document was the now-removed planted "Office Wi-Fi" doc. The held-out
set composition is unchanged otherwise.

### ITEM 2 — Paraphrase question audit

Ran `scripts/audit_paraphrase.py` on all 4 `paraphrase`-labelled questions.
Rule: a "paraphrase" shares no distinctive content words (words other than
common function words and generic terms like "time", "open", "help",
"client", "visit", "service", "office", "hours", "close") with its expected
document's **title + key_facts**.

| id | split | question | shared content words | verdict |
|---|---|---|---|---|
| 14 | dev | where to leave my car | (none) | OK |
| 15 | dev | cost of initial visit | (none) | OK |
| 16 | dev | no-show penalty | penalty, missed, appointment | BREAKS RULE |
| 34 | heldout | when is the office open on fridays | friday | BREAKS RULE |
| 35 | heldout | new client paperwork checklist | new, client | BREAKS RULE |

Three questions relabelled from `paraphrase` → `normal`:
- q16 (dev): `"no-show penalty"` → `normal`
- q34 (heldout): `"when is the office open on fridays"` → `normal`
- q35 (heldout): `"new client paperwork checklist"` → `normal`

The held-out set now has **0** paraphrase questions (was 2). The remaining
two paraphrase questions (q14, q15) satisfy the rule and both score 0% on
retrieval — the honest keyword-index limit.

After relabelling, offline eval (threshold 2.6, dev-tuned):

| Set | hit@1 | hit@3 | correct_refusal |
|---|---|---|---|
| held-out (19 questions) | 0.500 (7/14) | 0.643 (9/14) | 1.000 (5/5) |

By type on held-out:
- `normal` (n=12): hit@1 0.583 (7/12), hit@3 0.750 (9/12)
- `misspelled` (n=2): hit@1 0.000 (0/2), hit@3 0.000 (0/2)
- `unanswerable` (n=5): correct_refusal 1.000 (5/5)

Dev set (for reference): `normal` n=11 hit@1 0.727 (8/11) hit@3 1.000 (11/11);
`misspelled` n=3 hit@1 0.333 (1/3) hit@3 0.667 (2/3); `paraphrase` n=2
hit@1 0.000 (0/2) hit@3 0.000 (0/2); `unanswerable` n=5 refused 0.600 (3/5).

Threshold remained 2.6, retuned on dev set only. The held-out set was
inspected and changed: 3 questions relabelled, shrinking held-out paraphrase
from 2 to 0, growing held-out normal from 10 to 12.

### ITEM 3 — Live injection tests

`GROQ_API_KEY` was set (yes).

Groq documentation check:
- Model `openai/gpt-oss-120b`: confirmed available on free tier, **supports
  tool calling** (source: Groq Console model page; [models docs](https://console.groq.com/docs/models) list it under "Tool Calling" models)
- Free-tier rate limits (source: [rate limits docs](https://console.groq.com/docs/rate-limits)):
  - `openai/gpt-oss-120b`: 30 RPM, 1,000 RPD, 8K TPM, **200K TPD**
  - `openai/gpt-oss-20b`: 30 RPM, 1,000 RPD, 8K TPM, 200K TPD

Ran `pytest tests/test_prompt_injection.py` once with the new API key:
- **15 passed, 0 failed** in 102.72s
- All 7 live tests passed (the 7 `live`-marked tests in the file)
- No rate limit retries needed on this run
- Every test verified: planted instruction in a retrieved document does not
  cause a write; code-level guards (confirmation checks, validation) prevent
  the write regardless

### ITEM 4 — Three-way live comparison — **INVALID, to be re-run**

The attempt to compare the three `kb_mode`s on the held-out set ran out of the
Groq free-tier daily token budget partway through. Rate-limit (429) failures
were scored as wrong answers rather than excluded, so the numbers below
describe a partially-failed run, not the behaviour of the modes. **They are
not usable and are retained only to document what happened. Re-run all three
modes before drawing any conclusion.**

| Mode | Questions | key_fact_correct | correct_refusal | confident_wrong | avg_latency_ms | p95_latency_ms | avg_prompt_tokens | avg_completion_tokens | total_tokens |
|---|---|---|---|---|---|---|---|---|---|
| none | 19 | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** |
| full_prompt | 19 | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** | **INVALID** |
| rag | — | not run (TPD limit) | — | — | — | — | — | — | — |

No comparison between `none`, `full_prompt` and `rag` is recorded for this
phase. The offline retrieval numbers below are the only valid retrieval
results.

### Offline retrieval results (valid)

From `scripts/eval_kb.py`, threshold 2.6 (tuned on the dev set only):

| Set | hit@1 | hit@3 | correct_refusal |
|---|---|---|---|
| held-out (19 questions) | 0.500 (7/14) | 0.643 (9/14) | 1.000 (5/5) |

These numbers are recorded in `eval_results` (eval_name='kb_hit_rate').

## How to resume the three-way comparison

Three `kb_mode` runs are outstanding: **`rag`** (never completed), plus
**re-runs of `none`** and **`full_prompt`** (the previous pair is invalid, see
above). Each mode must be run once, on the held-out set, with the threshold
fixed at **2.6** (dev-tuned; do not retune on held-out).

Run order — cheapest first, so a budget shortfall costs the least:

1. `kb_mode=none` — smallest prompt, fewest tokens.
2. `kb_mode=full_prompt` — slightly larger prompt.
3. `kb_mode=rag` — largest prompt (retrieved documents are added per turn),
   so it needs the most remaining daily budget.

Budget: the Groq free tier allows **200,000 tokens per day** for
`openai/gpt-oss-120b`. A full three-mode run over 19 held-out questions costs
roughly 150k–200k tokens in total, so the three modes may not fit in one
calendar day on the free tier. Split them across days, or use a paid tier.

### Known gaps to fix before the next live run

Both gaps below are the reason the previous attempt produced invalid numbers.
They are **not** fixed yet.

1. **The eval script does not save progress per question.**
   `scripts/eval_kb_live.py` accumulates every question's result in memory and
   writes to `eval_results` only *after* all three modes have finished. A run
   that dies partway through a mode (rate limit, crash, interruption) loses
   that mode's work entirely, and re-running it re-spends the tokens. The
   script prints each question to stdout as it goes, so the console output is
   the only record of a partial run.

2. **Rate-limit errors are scored as "incorrect", not classified as "error".**
   When a call raises a 429, the script catches it, substitutes
   `reply = "[ERROR: ...]"`, and then scores that string exactly like any
   other reply: for an answerable question it counts as a wrong answer, and
   for an unanswerable question it counts as a confident-but-wrong refusal
   failure. Rate-limited questions therefore pull the metrics down instead of
   being excluded from them, which is what made the previous run's numbers
   meaningless. Before the next live run, the script needs to detect a
   429/rate-limit exception, mark that question as `error` (neither correct
   nor incorrect), and exclude it from the denominators — and, ideally, abort
   the mode with its partial progress saved instead of charging through the
   remaining questions.

## Notes

- The live eval initially reported 0% across every mode. The cause was not the
  retrieval: the default Groq model `llama-3.3-70b-versatile` has been retired
  by Groq and returns `404 model_not_found`, and the eval counted the failed
  call as a wrong answer. The default is now `openai/gpt-oss-120b`, which is
  the only tool-calling model on the free tier — `gpt-oss-20b` and
  `qwen3.8-27b` are available but answer without calling tools.
- `full_prompt` mode is a baseline, not a competitor: it can only answer the
  eleven hardcoded questions in the prompt. Whether it beats `none` or loses
  to `rag` on the held-out set is **not yet measured** — see ITEM 4 above.

## Files to create / modify

```
knowledge/
├── 01-hours.md
├── 02-location-parking.md
├── ...
├── 35-payment-methods.md
scripts/
├── index_kb.py          # Indexer
├── eval_kb.py           # Offline evaluation
└── eval_kb_live.py      # Optional live evaluation (needs GROQ_API_KEY)
backend/app/
├── config.py            # + VOICECHAT_KB_MODE, VOICECHAT_KB_THRESHOLD, VOICECHAT_KB_MAX_RESULTS
├── knowledge/
│   ├── __init__.py
│   ├── interface.py     # KnowledgeSearch protocol
│   └── fts_search.py    # FtsKnowledgeSearch implementation
├── tools/
│   ├── __init__.py      # + search_knowledge export
│   ├── knowledge.py     # search_knowledge tool, Pydantic models
│   └── decorator.py     # (no change; @logged_tool already handles it)
├── agent/
│   ├── schemas.py       # + search_knowledge tool definition
│   ├── prompts/
│   │   ├── system.md    # Updated with kb_mode template
│   │   └── system_rag.md# System prompt for rag mode (included by template)
│   ├── loop.py          # (no change; tool comes from schemas)
│   └── groq_client.py   # (no change)
tests/
├── test_knowledge.py    # Indexer, search, threshold, sanitization, logging
└── test_prompt_injection.py  # + planted instruction test
schema.sql               # + FTS5 virtual table, triggers, SCHEMA_VERSION=3
eval/
├── questions.csv        # 40 questions, types, expected doc ids, key facts
└── questions_dev.csv    # Dev split (or single file with split column)
```

## Known limitations

- **BM25 is not semantic**. "parking lot" won't match "where do I leave my car"
  unless both terms appear. The paraphrase and misspelling test cases measure
  this gap honestly.
- **No cross-language**. English only.
- **Threshold is corpus-dependent**. Re-tune if documents change significantly.
- **FTS5 external content** means `DELETE` on `faq_documents` requires the
  trigger to clean the index. The indexer uses `UPDATE ... SET active=0` for
  deactivation instead of `DELETE` to keep rowids stable; a full `--rebuild`
  recreates the virtual table.
- **No re-ranking**. Top 3 by BM25 only. Good enough for this phase.
- **The model must be able to call tools.** The live eval and the `live`
  pytest cases both depend on a Groq model that supports tool calling. If
  `VOICECHAT_GROQ_MODEL` points at a model that answers without calling
  tools, the agent silently never reaches the knowledge base and every
  retrieval metric reads as zero.