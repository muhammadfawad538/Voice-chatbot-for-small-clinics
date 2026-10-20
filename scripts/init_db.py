"""Build the database from schema.sql.

Works from the repo root both as a script and as a module:

    python scripts/init_db.py --reset
    python -m scripts.init_db --reset

Prints the resulting tables and the schema_version row. Exits non-zero on
failure. The database is disposable development data, so --reset is safe.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from app.config import get_settings
from app.db import SCHEMA_VERSION, init_db


def _reset(path: Path) -> None:
    """Delete the database and its WAL/SHM sidecars."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
            print(f"removed {candidate}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the voice-chatbot database.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the existing database before creating it",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    db_path = settings.db_path

    if args.reset:
        _reset(db_path)

    try:
        tables = init_db(db_path)
    except Exception as exc:
        print(f"error: could not initialise {db_path}: {exc}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("SELECT version FROM schema_version").fetchone()
    finally:
        conn.close()

    print(f"database: {db_path}")
    print(f"tables ({len(tables)}): {', '.join(tables)}")
    print(f"schema_version: {version[0] if version else 'MISSING'} (expected {SCHEMA_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
