"""Shared fixtures: an isolated database per test and a TestClient."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `import app...` and `import scripts...` work regardless of how pytest was
# invoked. pyproject sets pythonpath = ["backend", "."] for the normal case; this
# keeps bare `pytest` and `python -m pytest` working too.
REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT / "backend"), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import EXPECTED_TABLES, init_db  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh, initialised database in a temp directory.

    Settings are pointed at it, because the mock calendar client opens its own
    connection from ``settings.db_path`` — the same way a real provider client
    would manage its own storage.
    """
    path = tmp_path / "test.db"
    init_db(path)

    from app.config import settings as cached_settings

    previous = cached_settings.database_url
    cached_settings.database_url = str(path)
    try:
        yield path
    finally:
        cached_settings.database_url = previous


@pytest.fixture
def client(tmp_path: Path):
    """TestClient bound to a temp database and temp DATABASE_URL."""
    path = tmp_path / "test.db"
    init_db(path)

    from app.config import settings as cached_settings

    cached_settings.database_url = str(path)
    cached_settings.cors_origins = "http://localhost:3000"

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def expected_tables() -> frozenset[str]:
    return EXPECTED_TABLES
