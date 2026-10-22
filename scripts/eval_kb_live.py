#!/usr/bin/env python3
"""Live evaluation of knowledge base search with a real LLM.

Runs three kb_modes (none, full_prompt, rag) on the held-out question set.
Reports key-fact correctness, correct refusal, confident-but-wrong, and
average latency per mode. Requires GROQ_API_KEY.

Usage:
    GROQ_API_KEY=... python scripts/eval_kb_live.py

Results are written to the eval_results table (eval_name='kb_live').
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import AgentLoop
from app.agent.groq_client import GroqClient
from app.config import get_settings
from app.db import connect as db_connect

# Rate limit handling: max retries for 429 errors
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_BASE = 5  # seconds

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "voice-chatbot.db"
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.csv"

MODES = ("none", "full_prompt", "rag")
ANSWERABLE_TYPES = ("normal", "misspelled", "paraphrase")
ALL_TYPES = ANSWERABLE_TYPES + ("unanswerable",)


def load_questions(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


def extract_key_facts(text: str) -> set[str]:
    """Extract key facts from expected answer (pipe-separated)."""
    return {f.strip().lower() for f in text.split("|") if f.strip()}


def check_key_facts_in_reply(reply: str, key_facts: set[str]) -> float:
    """Return fraction of key facts mentioned in the reply."""
    if not key_facts:
        return 1.0
    reply_lower = reply.lower()
    found = sum(1 for kf in key_facts if kf in reply_lower)
    return found / len(key_facts)


def evaluate_mode(mode: str, questions: list[dict], db_path: Path) -> dict:
    """Run one kb_mode over all questions and return metrics."""
    settings = get_settings()
    settings.__dict__["voicechat_kb_mode"] = mode

    llm = GroqClient()
    results_by_type: dict[str, list[dict]] = {t: [] for t in ALL_TYPES}

    for q in questions:
        qtype = q["type"]
        key_facts = extract_key_facts(q.get("key_facts", ""))

        agent = AgentLoop(
            caller_number="(555) 123-4567",
            caller_number_masked="(XXX) XXX-4567",
            llm_client=llm,
            db_path=db_path,
        )

        start = time.perf_counter()
        # Retry on rate limit errors
        rate_limit_retries = 0
        while True:
            try:
                reply = agent.receive(q["question"])
                break
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and rate_limit_retries < MAX_RATE_LIMIT_RETRIES:
                    rate_limit_retries += 1
                    wait_time = RATE_LIMIT_BACKOFF_BASE * (2 ** (rate_limit_retries - 1))
                    msg = f"    [RATE LIMIT] Retrying in {wait_time}s "
                    msg += f"(attempt {rate_limit_retries}/{MAX_RATE_LIMIT_RETRIES})"
                    print(msg, flush=True)
                    time.sleep(wait_time)
                    continue
                reply = f"[ERROR: {e}]"
                break
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Capture token usage from agent state
        prompt_tokens = agent.state.total_prompt_tokens
        completion_tokens = agent.state.total_completion_tokens

        # Use ASCII-safe repr for Windows console
        safe_reply = reply[:70].encode("ascii", "replace").decode("ascii")
        print(
            f"  [{qtype:<12}] {q['question'][:52]:<52} -> {safe_reply!r}",
            flush=True,
        )

        # Evaluate
        key_fact_score = check_key_facts_in_reply(reply, key_facts)
        handoff = "transfer" in reply.lower() or "human" in reply.lower()
        offered_message = "take a message" in reply.lower()

        is_answerable = qtype in ANSWERABLE_TYPES
        is_unanswerable = qtype == "unanswerable"

        correct = False
        confident_wrong = False

        if is_answerable:
            # Correct if key facts present (threshold 0.5)
            correct = key_fact_score >= 0.5
            # Confident-wrong: high key-fact score but wrong doc (we don't track doc here)
            # Simplified: confident-wrong = NOT correct AND key_fact_score > 0
            confident_wrong = (not correct) and (key_fact_score > 0)
        elif is_unanswerable:
            # Correct if agent refuses / offers message / hands off
            correct = not handoff and offered_message
            # Confident-wrong: gives a specific answer instead of refusing
            confident_wrong = not correct and len(reply) > 20

        results_by_type[qtype].append({
            "question": q["question"],
            "reply": reply[:200],
            "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "key_fact_score": round(key_fact_score, 3),
            "correct": correct,
            "confident_wrong": confident_wrong,
            "handoff": handoff,
            "offered_message": offered_message,
        })

    # Aggregate
    summary: dict[str, dict] = {}
    for qtype, entries in results_by_type.items():
        n = len(entries)
        if n == 0:
            summary[qtype] = {"n": 0}
            continue
        latencies = [e["latency_ms"] for e in entries]
        latencies.sort()
        p95_idx = max(0, int(0.95 * n) - 1)
        p95_latency_ms = latencies[p95_idx] if latencies else 0
        summary[qtype] = {
            "n": n,
            "key_fact_correct": round(sum(e["correct"] for e in entries) / n, 3),
            "confident_wrong": round(sum(e["confident_wrong"] for e in entries) / n, 3),
            "avg_latency_ms": round(sum(latencies) / n, 1),
            "p95_latency_ms": p95_latency_ms,
            "avg_prompt_tokens": round(sum(e["prompt_tokens"] for e in entries) / n, 1),
            "avg_completion_tokens": round(sum(e["completion_tokens"] for e in entries) / n, 1),
            "total_tokens": sum(e["prompt_tokens"] + e["completion_tokens"] for e in entries),
            "correct_refusal": round(
                sum(1 for e in entries if e.get("offered_message")) / n, 3
            ) if qtype == "unanswerable" else None,
        }
    return {"by_type": summary, "details": results_by_type}


def main() -> int:
    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY not set", file=sys.stderr)
        return 1

    # Ensure DB exists and is indexed
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Run scripts/index_kb.py first.", file=sys.stderr)
        return 1

    questions = load_questions(QUESTIONS_PATH)
    heldout_questions = [q for q in questions if q["split"] == "heldout"]

    print(f"Held-out questions: {len(heldout_questions)}")
    print(f"Modes: {', '.join(MODES)}")

    all_results = {}

    for mode in MODES:
        print(f"\n=== Evaluating kb_mode={mode} ===", flush=True)
        result = evaluate_mode(mode, heldout_questions, DB_PATH)
        all_results[mode] = result

        # Print summary
        for qtype in ALL_TYPES:
            s = result["by_type"].get(qtype)
            if not s or s["n"] == 0:
                continue
            line = (
                f"  {qtype:<13} n={s['n']:<3} "
                f"key_fact_correct={s['key_fact_correct']:<6} "
                f"confident_wrong={s['confident_wrong']:<6} "
                f"avg_latency_ms={s['avg_latency_ms']:<7.1f} "
                f"p95_latency_ms={s['p95_latency_ms']:<7} "
                f"avg_prompt_tokens={s['avg_prompt_tokens']:<7.1f} "
                f"avg_completion_tokens={s['avg_completion_tokens']:<7.1f} "
                f"total_tokens={s['total_tokens']}"
            )
            if s.get("correct_refusal") is not None:
                line += f" correct_refusal={s['correct_refusal']}"
            print(line)

    # Overall comparison
    print("\n=== Summary ===")
    for mode in MODES:
        r = all_results[mode]["by_type"]
        ans = [s for t in ANSWERABLE_TYPES for s in [r[t]] if s["n"]]
        n_ans = sum(s["n"] for s in ans)
        unans = r["unanswerable"]

        kf_correct = sum(s["key_fact_correct"] * s["n"] for s in ans) / n_ans if n_ans else 0
        cw = sum(s["confident_wrong"] * s["n"] for s in ans) / n_ans if n_ans else 0
        lat = sum(s["avg_latency_ms"] * s["n"] for s in ans) / n_ans if n_ans else 0
        p95_vals = [s["p95_latency_ms"] for s in ans if s["p95_latency_ms"]]
        p95_overall = max(p95_vals) if p95_vals else 0
        total_prompt = sum(s["avg_prompt_tokens"] * s["n"] for s in ans) / n_ans if n_ans else 0
        total_comp = sum(s["avg_completion_tokens"] * s["n"] for s in ans) / n_ans if n_ans else 0
        total_tok = sum(s["total_tokens"] for s in ans)
        refusal = unans.get("correct_refusal", 0) if unans["n"] else 0

        print(
            f"  {mode:<13} key_fact_correct={kf_correct:.3f} "
            f"confident_wrong={cw:.3f} correct_refusal={refusal:.3f} "
            f"avg_latency_ms={lat:.1f} p95_latency_ms={p95_overall} "
            f"avg_prompt_tokens={total_prompt:.1f} avg_completion_tokens={total_comp:.1f} "
            f"total_tokens={total_tok}"
        )

    # Write to eval_results
    conn = db_connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for mode, result in all_results.items():
            r = result["by_type"]
            ans = [s for t in ANSWERABLE_TYPES for s in [r[t]] if s["n"]]
            n_ans = sum(s["n"] for s in ans)
            unans = r["unanswerable"]

            kf_correct = sum(s["key_fact_correct"] * s["n"] for s in ans) / n_ans if n_ans else 0
            cw = sum(s["confident_wrong"] * s["n"] for s in ans) / n_ans if n_ans else 0
            lat = sum(s["avg_latency_ms"] * s["n"] for s in ans) / n_ans if n_ans else 0
            p95_vals = [s["p95_latency_ms"] for s in ans if s["p95_latency_ms"]]
            p95_overall = max(p95_vals) if p95_vals else 0
            total_prompt = (
                sum(s["avg_prompt_tokens"] * s["n"] for s in ans) / n_ans if n_ans else 0
            )
            total_comp = (
                sum(s["avg_completion_tokens"] * s["n"] for s in ans) / n_ans if n_ans else 0
            )
            total_tok = sum(s["total_tokens"] for s in ans)
            refusal = unans.get("correct_refusal", 0) if unans["n"] else 0

            conn.execute(
                """INSERT INTO eval_results (eval_name, case_id, passed, details_json, created_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (
                    "kb_live",
                    f"kb_live_{mode}",
                    json.dumps({
                        "kb_mode": mode,
                        "key_fact_correct": round(kf_correct, 3),
                        "confident_wrong": round(cw, 3),
                        "correct_refusal": round(refusal, 3),
                        "avg_latency_ms": round(lat, 1),
                        "p95_latency_ms": p95_overall,
                        "avg_prompt_tokens": round(total_prompt, 1),
                        "avg_completion_tokens": round(total_comp, 1),
                        "total_tokens": total_tok,
                        "by_type": {t: r[t] for t in ALL_TYPES},
                    }),
                    datetime.now(UTC).isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    print("\nResults written to eval_results (eval_name='kb_live').")
    return 0


if __name__ == "__main__":
    sys.exit(main())