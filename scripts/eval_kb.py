#!/usr/bin/env python3
"""Offline evaluation of knowledge base search.

Reports hit rate @1 and @3 on the held-out set, broken down by question
type (normal, misspelled, paraphrase, unanswerable). Tunes the confidence
threshold on the dev set and writes the results to the ``eval_results``
table.

Usage:
    python scripts/eval_kb.py

No LLM and no network. Uses the existing SQLite database and the documents
in ``knowledge/``.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.fts_search import create_fts_search
from app.knowledge.interface import SearchResult

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "voice-chatbot.db"
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.csv"

ANSWERABLE_TYPES = ("normal", "misspelled", "paraphrase")
ALL_TYPES = ANSWERABLE_TYPES + ("unanswerable",)

# The grid the threshold is searched over.
THRESHOLD_MIN = 0.0
THRESHOLD_MAX = 6.0
THRESHOLD_STEP = 0.1


def load_questions(path: Path) -> list[dict]:
    """Load the question set from CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


def load_title_map(db_path: Path) -> dict[int, str]:
    """Map numeric document id -> title, so a question's expected id can be
    compared against the titles that search returns."""
    conn = sqlite3.connect(str(db_path))
    try:
        return dict(conn.execute("SELECT id, title FROM faq_documents").fetchall())
    finally:
        conn.close()


def search(question: str, threshold: float) -> SearchResult:
    """Run one search against the current index."""
    return create_fts_search().search(question, max_results=3, threshold=threshold)


def evaluate(questions: list[dict], threshold: float, title_map: dict[int, str]) -> dict:
    """Evaluate a question set at one threshold.

    Two things have to be right for an answerable question to count as a
    success: the search must report ``found=true`` (the top score cleared
    the threshold) *and* the expected document must be in the results.
    An unanswerable question succeeds when the search reports ``found=false``.
    """
    by_type: dict[str, list[dict]] = {t: [] for t in ALL_TYPES}

    for q in questions:
        expected_id = int(q.get("expected_doc_id") or 0)
        expected_title = title_map.get(expected_id) if expected_id else None
        res = search(q["question"], threshold)

        sources = [r.source for r in res.results]
        top = res.results[0] if res.results else None
        by_type[q["type"]].append({
            "question": q["question"],
            "expected": expected_title,
            "found": res.found,
            "hit_at_1": bool(expected_title and res.found and sources[:1] == [expected_title]),
            "hit_at_3": bool(expected_title and res.found and expected_title in sources),
            "top_score": top.score if top else 0.0,
            "top_source": sources[0] if sources else None,
        })

    summary: dict[str, dict] = {}
    for qtype, entries in by_type.items():
        n = len(entries)
        if n == 0:
            summary[qtype] = {"n": 0}
            continue
        summary[qtype] = {
            "n": n,
            # Hit rates are over answerable questions only.
            "hit_at_1": round(sum(e["hit_at_1"] for e in entries) / n, 3),
            "hit_at_3": round(sum(e["hit_at_3"] for e in entries) / n, 3),
            # A refusal is correct only for an unanswerable question.
            "correct_refusal": round(
                sum(1 for e in entries if not e["found"]) / n, 3
            ) if qtype == "unanswerable" else None,
            "found_percent": round(sum(e["found"] for e in entries) / n, 3),
            "avg_score": round(sum(e["top_score"] for e in entries) / n, 3),
        }
    return summary


def tune_threshold(dev_questions: list[dict], title_map: dict[int, str]) -> dict:
    """Search the threshold grid on the dev set.

    The objective is the number of correct *decisions*: an answerable
    question is correct when it is found and the expected document is in
    the results, an unanswerable question is correct when it is refused.
    Maximising hit rate alone is not usable — it only ever rises as the
    threshold drops, so it would always select the lowest threshold in the
    grid and refuse nothing.

    Ties are broken toward the stricter threshold: a wrong answer given
    confidently to a caller is worse than a refusal that routes them to a
    human.
    """
    [q for q in dev_questions if q["type"] in ANSWERABLE_TYPES]
    unanswerable = [q for q in dev_questions if q["type"] == "unanswerable"]

    best = None
    step = int(round(THRESHOLD_STEP * 10))
    for raw in range(int(THRESHOLD_MIN * 10), int(THRESHOLD_MAX * 10) + 1, step):
        t = round(raw / 10, 1)
        summary = evaluate(dev_questions, t, title_map)

        correct = 0
        for qtype in ANSWERABLE_TYPES:
            s = summary[qtype]
            correct += round(s["hit_at_3"] * s["n"])
        if unanswerable:
            s = summary["unanswerable"]
            correct += round(s["correct_refusal"] * s["n"])

        total = len(dev_questions)
        rate = correct / total if total else 0.0
        if best is None or rate > best["rate"] or (rate == best["rate"] and t > best["threshold"]):
            best = {"threshold": t, "rate": rate, "correct": correct, "total": total}

    return best


def print_summary(label: str, summary: dict, threshold: float) -> None:
    print(f"\n{label} (threshold={threshold}):")
    for qtype in ALL_TYPES:
        s = summary.get(qtype)
        if not s or s["n"] == 0:
            continue
        line = (
            f"  {qtype:<13} n={s['n']:<3} "
            f"hit@1={s['hit_at_1']:<6} hit@3={s['hit_at_3']:<6} "
            f"found%={s['found_percent']:<6} avg_score={s['avg_score']}"
        )
        if s.get("correct_refusal") is not None:
            line += f" refused%={s['correct_refusal']}"
        print(line)


def main() -> int:
    title_map = load_title_map(DB_PATH)
    questions = load_questions(QUESTIONS_PATH)
    dev_questions = [q for q in questions if q["split"] == "dev"]
    heldout_questions = [q for q in questions if q["split"] == "heldout"]

    print(f"Dev questions: {len(dev_questions)}")
    print(f"Held-out questions: {len(heldout_questions)}")

    best = tune_threshold(dev_questions, title_map)
    threshold = best["threshold"]
    print(f"\nTuned threshold on dev set: {threshold}")
    print(f"  correct decisions on dev: {best['correct']}/{best['total']} ({best['rate']:.3f})")

    dev_summary = evaluate(dev_questions, threshold, title_map)
    print_summary("Dev set", dev_summary, threshold)

    heldout_summary = evaluate(heldout_questions, threshold, title_map)
    print_summary("Held-out set", heldout_summary, threshold)

    # Overall numbers for the held-out set.
    ans = [s for t in ANSWERABLE_TYPES for s in [heldout_summary[t]] if s["n"]]
    n_ans = sum(s["n"] for s in ans)
    hit1 = sum(s["hit_at_1"] * s["n"] for s in ans) / n_ans if n_ans else 0.0
    hit3 = sum(s["hit_at_3"] * s["n"] for s in ans) / n_ans if n_ans else 0.0
    unans = heldout_summary["unanswerable"]
    refusal = unans["correct_refusal"] if unans["n"] else 0.0

    print(f"\nHeld-out overall: hit@1={hit1:.3f} hit@3={hit3:.3f} "
          f"correct_refusal={refusal:.3f}")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """INSERT INTO eval_results (eval_name, case_id, passed, details_json, created_at)
               VALUES (?, ?, 1, ?, ?)""",
            (
                "kb_hit_rate",
                f"kb_heldout_{threshold}",
                json.dumps({
                    "threshold": threshold,
                    "hit_at_1": round(hit1, 3),
                    "hit_at_3": round(hit3, 3),
                    "correct_refusal": refusal,
                    "dev_correct_decisions": best["correct"],
                    "dev_total": best["total"],
                    "by_type": {t: heldout_summary[t] for t in ALL_TYPES},
                }),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print("\nResults written to eval_results (eval_name='kb_hit_rate').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
