#!/usr/bin/env python3
"""Audit the "paraphrase" questions in eval/questions.csv.

A paraphrase question is supposed to name the same thing as its expected
document *without* sharing distinctive vocabulary with it — that is the whole
point, because a keyword index should fail on it. This script lists the
content words each paraphrase question shares with its expected document's
title and key facts, so the labelling can be checked against the data rather
than against whether retrieval happened to succeed.

Usage:
    python scripts/audit_paraphrase.py
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "voice-chatbot.db"
QUESTIONS_PATH = REPO_ROOT / "eval" / "questions.csv"

# Common function words. These carry no topic information in either the
# question or the document, so they never count as an overlap.
FUNCTION_WORDS = frozenset({
    "a", "am", "an", "and", "are", "as", "at", "be", "been", "being", "but",
    "by", "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "how", "i", "if", "in", "into", "is", "it", "its", "may", "me",
    "might", "must", "my", "no", "not", "of", "on", "or", "our", "out", "shall",
    "should", "so", "than", "that", "the", "their", "there", "they", "this",
    "to", "up", "us", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "would", "you", "your",
})

# Generic business-service vocabulary. These words appear across the
# documents as boilerplate ("office hours", "your visit", "service") and would
# make a question look like a paraphrase when the only thing it shares with
# the document is a word every business document contains.
GENERIC_TERMS = frozenset({
    "business", "call", "calling", "client", "clients", "close", "closes",
    "cost", "costs", "day", "days", "details", "help", "hour", "hours",
    "initial", "issue", "issues", "need", "needs", "office", "open", "person",
    "people", "place", "policy", "price", "prices", "question", "questions",
    "service", "services", "tell", "time", "times", "visit", "visits", "want",
    "week", "weeks",
})


def tokenize(text: str) -> list[str]:
    """Lowercase and split into alphabetic tokens."""
    return re.findall(r"[a-z0-9]+", text.lower())


def normalize(token: str) -> str:
    """Fold a simple plural so 'fridays' and 'friday' compare equal."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def content_words(text: str) -> set[str]:
    """Tokens that are not function words or generic terms, plural-folded."""
    out = set()
    for token in tokenize(text):
        if token in FUNCTION_WORDS or token in GENERIC_TERMS:
            continue
        folded = normalize(token)
        if folded in FUNCTION_WORDS or folded in GENERIC_TERMS:
            continue
        out.add(folded)
    return out


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id,title FROM faq_documents")}

    with open(QUESTIONS_PATH, newline="", encoding="utf-8") as f:
        questions = [dict(r) for r in csv.DictReader(f)]

    rows = []
    for q in questions:
        if q["type"] != "paraphrase":
            continue
        expected_id = int(q["expected_doc_id"] or 0)
        title = titles.get(expected_id, "")
        key_facts = q.get("key_facts", "")

        doc_words = content_words(f"{title} {key_facts}")
        q_words = content_words(q["question"])
        shared = sorted(q_words & doc_words)

        rows.append({
            "id": q["id"],
            "split": q["split"],
            "question": q["question"],
            "doc_id": expected_id,
            "doc_title": title,
            "key_facts": key_facts,
            "shared": shared,
            "verdict": "OK" if not shared else "BREAKS RULE",
        })

    conn.close()

    # Table
    print(f"{'id':<4}{'split':<9}{'shared content words':<38}{'verdict'}")
    print("-" * 100)
    for r in rows:
        shared = ", ".join(r["shared"]) if r["shared"] else "(none)"
        print(f"{r['id']:<4}{r['split']:<9}{shared:<38}{r['verdict']}")
    print()

    for r in rows:
        print(f"--- q{r['id']} ({r['split']}) {r['question']}")
        print(f"    expected doc: #{r['doc_id']} {r['doc_title']}")
        print(f"    key facts:    {r['key_facts']}")
        print(f"    shared:       {r['shared'] or '(none)'}")
        print()

    breaking = [r for r in rows if r["verdict"] != "OK"]
    print(f"{len(rows) - len(breaking)}/{len(rows)} paraphrase questions satisfy the rule; "
          f"{len(breaking)} break it.")

    # Also report every other question type, for context.
    print("\nFor contrast, shared content words for the other types:")
    for q in questions:
        if q["type"] == "paraphrase":
            continue
        expected_id = int(q["expected_doc_id"] or 0)
        if not expected_id:
            continue
        title = titles.get(expected_id, "")
        doc_words = content_words(f"{title} {q.get('key_facts','')}")
        shared = sorted(content_words(q["question"]) & doc_words)
        print(f"  {q['id']:<4}{q['type']:<13}{q['split']:<9}{', '.join(shared) or '(none)'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
