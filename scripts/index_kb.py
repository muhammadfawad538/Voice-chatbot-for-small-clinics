#!/usr/bin/env python3
"""Index knowledge base documents into SQLite + FTS5.

Usage:
    python scripts/index_kb.py           # incremental update
    python scripts/index_kb.py --rebuild # drop and recreate FTS, full reindex
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# Repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
DB_PATH = REPO_ROOT / "data" / "voice-chatbot.db"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_front_matter(path: Path) -> tuple[dict, str]:
    """Parse YAML-like front matter from a markdown file.

    Returns (metadata_dict, body_text).
    """
    text = path.read_text(encoding="utf-8")
    # Match --- front matter ---
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_text, body = match.groups()
    meta = {}
    for line in fm_text.strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip().strip('"')
    return meta, body.strip()


def doc_hash(title: str, body: str) -> str:
    """Stable hash for change detection."""
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:32]


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual table and sync triggers if missing."""
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS faq_fts USING fts5(
            title, body, category,
            content='faq_documents',
            content_rowid='id',
            tokenize='porter unicode61'
        );

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
    """)
    conn.commit()


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Drop and recreate FTS index, then repopulate from faq_documents."""
    # The sync triggers outlive the virtual table, so they go first.
    for trigger in ("faq_fts_ai", "faq_fts_ad", "faq_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS faq_fts")
    ensure_fts(conn)
    # Repopulate: external content table doesn't auto-populate on create,
    # so we insert each row.
    conn.execute("""
        INSERT INTO faq_fts(rowid, title, body, category)
        SELECT id, title, body, category FROM faq_documents WHERE active = 1
    """)
    conn.commit()


def index_documents(conn: sqlite3.Connection, rebuild: bool = False) -> dict:
    """Index all markdown files in knowledge/ directory.

    Returns stats dict.
    """
    if rebuild:
        rebuild_fts(conn)
    else:
        ensure_fts(conn)

    files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    stats = {"seen": 0, "inserted": 0, "updated": 0, "deactivated": 0}

    # Track which docs we saw this run
    seen_hashes: set[str] = set()
    seen_ids: set[int] = set()

    for path in files:
        stats["seen"] += 1
        meta, body = parse_front_matter(path)

        title = meta.get("title", path.stem)
        category = meta.get("category", "general")
        updated_str = meta.get("updated", datetime.now().date().isoformat())
        content_hash = doc_hash(title, body)

        # Check if exists by title (stable key)
        row = conn.execute(
            "SELECT id, content_hash, active FROM faq_documents WHERE title = ?",
            (title,),
        ).fetchone()

        if row:
            doc_id = row["id"]
            seen_ids.add(doc_id)
            if row["content_hash"] != content_hash:
                # Content changed
                conn.execute(
                    """
                    UPDATE faq_documents
                    SET body = ?, category = ?, updated = ?, content_hash = ?,
                        active = 1
                    WHERE id = ?
                    """,
                    (body, category, updated_str, content_hash, doc_id),
                )
                stats["updated"] += 1
            elif row["active"] == 0:
                # Was deactivated, now back
                conn.execute(
                    "UPDATE faq_documents SET active = 1 WHERE id = ?", (doc_id,)
                )
                stats["updated"] += 1
            # else: unchanged
        else:
            # New document
            cursor = conn.execute(
                """
                INSERT INTO faq_documents
                    (title, body, category, updated, active, content_hash, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    title,
                    body,
                    category,
                    updated_str,
                    content_hash,
                    datetime.now(UTC).isoformat(),
                ),
            )
            doc_id = cursor.lastrowid
            seen_ids.add(doc_id)
            stats["inserted"] += 1

        seen_hashes.add(content_hash)

    # Deactivate documents that weren't seen (deleted or renamed).
    # This runs on rebuild too: --rebuild reindexes the FTS table, but a
    # document removed from knowledge/ must still be deactivated, otherwise
    # it stays searchable forever.
    all_rows = conn.execute("SELECT id FROM faq_documents WHERE active = 1").fetchall()
    for row in all_rows:
        if row["id"] not in seen_ids:
            conn.execute(
                "UPDATE faq_documents SET active = 0 WHERE id = ?", (row["id"],)
            )
            stats["deactivated"] += 1

    conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Index knowledge base into SQLite FTS5")
    parser.add_argument(
        "--rebuild", action="store_true", help="Drop and recreate FTS index, full reindex"
    )
    args = parser.parse_args()

    if not KNOWLEDGE_DIR.exists():
        print(f"Knowledge directory not found: {KNOWLEDGE_DIR}")
        return 1

    conn = connect_db()
    try:
        stats = index_documents(conn, rebuild=args.rebuild)
    finally:
        conn.close()

    action = "Rebuilt" if args.rebuild else "Indexed"
    print(
        f"{action} KB: {stats['seen']} files, "
        f"{stats['inserted']} new, {stats['updated']} updated, "
        f"{stats['deactivated']} deactivated"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())