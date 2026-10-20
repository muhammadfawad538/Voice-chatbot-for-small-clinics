"""Health endpoint contract."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app import db as db_module
from app.db import check_schema, connect


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_health_response_shape(client: TestClient):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]
    assert _parse_ts(body["timestamp"]).tzinfo is not None


def test_health_503_when_db_unavailable(client: TestClient, monkeypatch, tmp_path):
    """An unreachable database yields a 503 that leaks no exception text."""

    def _boom(*args, **kwargs):
        raise RuntimeError("secret connection detail: user=postgres password=hunter2")

    monkeypatch.setattr(db_module, "connect", _boom)

    resp = client.get("/api/v1/health")
    assert resp.status_code == 503

    body = resp.json()
    assert body["status"] == "error"
    assert body["database"] == "error"
    # The reason is logged server-side, never returned.
    assert "secret connection detail" not in resp.text
    assert "hunter2" not in resp.text
    assert "Traceback" not in resp.text


def test_health_503_when_schema_missing_a_table(client: TestClient, monkeypatch):
    """A database missing a table is reported as unhealthy."""

    def _broken_schema(*args, **kwargs):
        return False, ["missing tables: tool_calls"]

    monkeypatch.setattr(db_module, "check_schema", _broken_schema)

    resp = client.get("/api/v1/health")
    assert resp.status_code == 503
    assert resp.json()["database"] == "error"


def test_check_schema_detects_drift(db_path, expected_tables):
    """Exact match: a dropped table is reported, an extra table is reported."""
    conn = connect(db_path)
    try:
        healthy, problems = check_schema(conn)
        assert healthy is True, problems

        conn.execute("DROP TABLE tool_calls")
        conn.execute("CREATE TABLE stray_table (id INTEGER)")
        conn.commit()

        healthy, problems = check_schema(conn)
        assert healthy is False
        assert any("missing tables" in problem for problem in problems)
        assert any("unexpected tables" in problem for problem in problems)
    finally:
        conn.close()


def test_root_lists_service(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["health"] == "/api/v1/health"
    assert body["docs"] == "/docs"
