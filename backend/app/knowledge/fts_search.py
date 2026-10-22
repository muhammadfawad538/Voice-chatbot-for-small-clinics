"""FTS5 (BM25) knowledge search implementation."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from app.db import connect as db_connect
from app.knowledge.interface import DocResult, KnowledgeSearchBase

if TYPE_CHECKING:
    from sqlite3 import Connection


# Stopwords for English question phrasing — kept small, only very common words
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "do", "does",
    "for", "from", "how", "i", "if", "in", "is", "it", "me", "my", "of", "on",
    "or", "the", "their", "there", "they", "this", "to", "us", "was", "we", "were",
    "what", "when", "where", "which", "who", "will", "with", "would", "you", "your",
})


class FtsKnowledgeSearch(KnowledgeSearchBase):
    """FTS5 BM25 search with query sanitization and per-token scoring."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path

    def _get_conn(self) -> Connection:
        return db_connect(self._db_path)

    def _sanitize(self, query: str) -> list[str]:
        """Extract search tokens from caller query.

        - Lowercase, strip FTS5 syntax characters
        - Remove stopwords and tokens shorter than 2 chars
        - Return unique tokens in original order (first occurrence wins)
        """
        if not query or not query.strip():
            return []
        # Replace FTS5 operators with spaces
        cleaned = re.sub(r'[^0-9A-Za-z\s]', ' ', query.lower())
        tokens = [t for t in cleaned.split() if t and t not in _STOPWORDS and len(t) > 1]
        # Deduplicate preserving order
        seen = set()
        out = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def _score_tokens(self, conn: Connection, tokens: list[str], limit: int) -> dict[int, float]:
        """Aggregate BM25 scores across tokens.

        Returns {doc_id: total_score}. Only docs matching at least one token.

        FTS5 defaults to AND between terms, so a multi-word query returns
        nothing. Each token is scored on its own and the scores are summed.
        Speech-to-text errors misspell the word outright ("hourz"), and a
        stemmer does not always catch them ("accessable"), so a token that
        matches nothing falls back to dropping its last character — once,
        not as a general edit-distance search.
        """
        if not tokens:
            return {}

        agg: defaultdict[int, float] = defaultdict(float)
        for token in tokens:
            rows = self._match(conn, token, limit)
            if not rows and len(token) >= 4:
                rows = self._match(conn, token[:-1], limit)
            for row in rows:
                # SQLite BM25 is negative; convert to positive for threshold logic
                agg[row["rowid"]] += max(-row["score"], 0.0)
        return dict(agg)

    @staticmethod
    def _match(conn: Connection, token: str, limit: int):
        """One prefix query. A bare token can never be FTS5 syntax."""
        return conn.execute(
            "SELECT rowid, bm25(faq_fts) AS score FROM faq_fts "
            "WHERE faq_fts MATCH ? ORDER BY bm25(faq_fts) LIMIT ?",
            (token + "*", limit),
        ).fetchall()

    def _fetch_docs(self, conn: Connection, doc_ids: list[int]) -> dict[int, DocResult]:
        """Fetch document details for a list of rowids."""
        if not doc_ids:
            return {}
        placeholders = ",".join("?" * len(doc_ids))
        rows = conn.execute(
            "SELECT id, title, body, category, updated FROM faq_documents "
            f"WHERE id IN ({placeholders}) AND active = 1",
            doc_ids,
        ).fetchall()
        return {
            row["id"]: DocResult(
                source=row["title"],
                score=0.0,  # filled in by caller
                updated=row["updated"],
                text=row["body"],
            )
            for row in rows
        }

    def _raw_search(self, query: str, limit: int) -> list[DocResult]:
        tokens = self._sanitize(query)
        if not tokens:
            return []

        conn = self._get_conn()
        try:
            scores = self._score_tokens(conn, tokens, limit)
            if not scores:
                return []

            # Sort by score descending, take top candidates
            sorted_ids = sorted(scores.keys(), key=lambda d: -scores[d])[:limit]
            docs = self._fetch_docs(conn, sorted_ids)

            # Attach scores and return in rank order
            out = []
            for doc_id in sorted_ids:
                if doc_id in docs:
                    doc = docs[doc_id]
                    out.append(DocResult(
                        source=doc.source,
                        score=scores[doc_id],
                        updated=doc.updated,
                        text=doc.text,
                    ))
            return out
        finally:
            conn.close()


def create_fts_search(db_path: str | None = None) -> FtsKnowledgeSearch:
    """Factory for dependency injection."""
    return FtsKnowledgeSearch(db_path)


__all__ = ["FtsKnowledgeSearch", "create_fts_search"]