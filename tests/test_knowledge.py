"""Tests for Phase 2c — Knowledge Base (SQLite FTS5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.index_kb import index_documents

from app.agent import AgentLoop, FakeLLMClient, LLMResponse, LLMUsage
from app.config import settings as cached_settings
from app.db import connect as db_connect
from app.db import init_db
from app.knowledge import DocResult, SearchResult, create_fts_search
from app.tools import search_knowledge
from app.tools.decorator import set_current_call_id
from app.tools.knowledge import SearchKnowledgeInput, SearchKnowledgeOutput

# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def text(content: str) -> LLMResponse:
    """A fake LLM response that only speaks."""
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="fake"),
        raw=None,
    )


def tool_call(name: str, args: dict | None = None) -> LLMResponse:
    """A fake LLM response that calls one tool."""
    return LLMResponse(
        content=None,
        tool_calls=[{
            "id": "tc_1",
            "name": name,
            "arguments": json.dumps(args or {}),
        }],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, model="fake"),
        raw=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path):
    """Fresh database for each test."""
    path = tmp_path / "test.db"
    init_db(path)

    previous = cached_settings.database_url
    cached_settings.database_url = str(path)
    try:
        yield path
    finally:
        cached_settings.database_url = previous


@pytest.fixture
def indexed_db(temp_db):
    """Database with knowledge docs indexed."""
    conn = db_connect(temp_db)
    try:
        stats = index_documents(conn, rebuild=True)
        # knowledge/ holds the 25 real FAQ documents. The planted-injection
        # document lives in tests/fixtures/ and is only indexed by the tests
        # that specifically need it.
        assert stats["inserted"] == 25
    finally:
        conn.close()
    return temp_db


# ---------------------------------------------------------------------------
# Indexer tests
# ---------------------------------------------------------------------------


def test_indexer_idempotency(indexed_db):
    """Re-running indexer with no changes is a no-op."""
    conn = db_connect(indexed_db)
    try:
        stats = index_documents(conn, rebuild=False)
        assert stats["inserted"] == 0
        assert stats["updated"] == 0
        assert stats["deactivated"] == 0
        assert stats["seen"] == 25
    finally:
        conn.close()


def test_indexer_rebuild(indexed_db):
    """--rebuild drops and recreates FTS, then repopulates.

    The documents already exist, so a rebuild reindexes them rather than
    inserting new rows. What matters is that the FTS index is repopulated and
    searchable again afterwards.
    """
    conn = db_connect(indexed_db)
    try:
        stats = index_documents(conn, rebuild=True)
        assert stats["inserted"] == 0
        assert stats["seen"] == 25
        # FTS index is repopulated from active documents
        count = conn.execute(
            "SELECT COUNT(*) FROM faq_fts"
        ).fetchone()[0]
        assert count == 25
    finally:
        conn.close()


def test_indexer_deactivates_removed(indexed_db, tmp_path):
    """Removing a markdown file deactivates its document."""
    import shutil

    # Build a knowledge dir that starts as 25 real FAQs plus the planted
    # fixture (26 files), then remove the fixture to exercise deactivation.
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    src = Path(__file__).resolve().parents[1] / "knowledge"
    for f in src.glob("*.md"):
        shutil.copy(f, knowledge_dir / f.name)

    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "26-planted-injection.md"
    )
    shutil.copy(fixture, knowledge_dir / fixture.name)

    # Monkey-patch the module's KNOWLEDGE_DIR
    import scripts.index_kb as ikb

    original = ikb.KNOWLEDGE_DIR
    ikb.KNOWLEDGE_DIR = knowledge_dir
    try:
        conn = db_connect(indexed_db)
        try:
            # First pass: 26 files seen, the fixture is inserted as new.
            stats = index_documents(conn, rebuild=True)
            assert stats["seen"] == 26
            assert stats["inserted"] == 1
            row = conn.execute(
                "SELECT active FROM faq_documents WHERE title = 'Office Wi-Fi'"
            ).fetchone()
            assert row is not None
            assert row["active"] == 1

            # Second pass: the fixture is gone, so its document is deactivated.
            (knowledge_dir / fixture.name).unlink()
            stats = index_documents(conn, rebuild=False)
            assert stats["seen"] == 25
            assert stats["deactivated"] == 1
            # The deactivated doc should have active=0
            row = conn.execute(
                "SELECT active FROM faq_documents WHERE title = 'Office Wi-Fi'"
            ).fetchone()
            assert row is not None
            assert row["active"] == 0
        finally:
            conn.close()
    finally:
        ikb.KNOWLEDGE_DIR = original


# ---------------------------------------------------------------------------
# FTS5 search tests
# ---------------------------------------------------------------------------


def test_known_queries_return_expected_docs(indexed_db):
    """Key queries return the right document at rank 1.

    Threshold 0.0 keeps these tests about ranking, not about the threshold,
    which the eval script tunes separately.
    """
    searcher = create_fts_search()

    # Hours query
    res = searcher.search("What are your hours?", max_results=3, threshold=0.0)
    assert res.found is True
    assert len(res.results) >= 1
    assert "Hours" in res.results[0].source

    # Parking query
    res = searcher.search("Where can I park?", max_results=3, threshold=0.0)
    assert res.found is True
    assert any("Parking" in r.source or "Location" in r.source for r in res.results)

    # Cancellation policy
    res = searcher.search("cancellation fee", max_results=3, threshold=0.0)
    assert res.found is True
    assert any("Cancellation" in r.source for r in res.results)

    # New client
    res = searcher.search("what do new clients need", max_results=3, threshold=0.0)
    assert res.found is True
    assert any("New Client" in r.source for r in res.results)


def test_below_threshold_returns_found_false(indexed_db):
    """Query with no good match returns found=false."""
    searcher = create_fts_search()
    # A threshold above anything this corpus can score.
    res = searcher.search("hours", max_results=3, threshold=100.0)
    assert res.found is False
    # But results are still returned (up to max_results)
    assert len(res.results) <= 3


def test_fts5_special_characters_do_not_raise(indexed_db):
    """FTS5 syntax characters in query don't cause errors."""
    searcher = create_fts_search()

    # These should not raise
    queries = [
        'what "are" your hours',
        "hours:parking",
        "hours -parking",
        "hours*",
        "hours^2",
        "hours@domain",
        "(hours)",
        "hours~",
    ]
    for q in queries:
        res = searcher.search(q, max_results=3, threshold=2.0)
        # Just verify it doesn't crash
        assert isinstance(res, SearchResult)


def test_retrievals_logged_with_masked_query(temp_db, indexed_db):
    """Every retrieval writes a row to the retrievals table."""
    conn = db_connect(indexed_db)
    try:
        # A real call row, so the FK on retrievals holds.
        conn.execute(
            "INSERT INTO calls (caller_number_masked, status, started_at) "
            "VALUES (?, 'active', '2026-10-20T12:00:00Z')",
            ("(XXX) XXX-4567",),
        )
        conn.commit()
        call_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        set_current_call_id(call_id)
        try:
            result = search_knowledge(
                conn, SearchKnowledgeInput(query="hours", confirmed=True)
            )
        finally:
            set_current_call_id(None)

        assert result["ok"] is True

        rows = conn.execute(
            "SELECT query, document_id, score, rank FROM retrievals "
            "WHERE call_id = ? ORDER BY rank",
            (call_id,),
        ).fetchall()
        assert len(rows) >= 1
        for row in rows:
            assert row["document_id"] > 0
            assert row["score"] >= 0
            assert row["rank"] >= 1
        # Ranks are 1..n with no gaps
        assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    finally:
        conn.close()


def test_retrievals_query_is_masked(temp_db, indexed_db):
    """A phone number in the query is masked before it is stored."""
    conn = db_connect(indexed_db)
    try:
        conn.execute(
            "INSERT INTO calls (caller_number_masked, status, started_at) "
            "VALUES (?, 'active', '2026-10-20T12:00:00Z')",
            ("(XXX) XXX-4567",),
        )
        conn.commit()
        call_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        set_current_call_id(call_id)
        try:
            search_knowledge(
                conn,
                SearchKnowledgeInput(
                    query="what are your hours, call me at (555) 123-4567",
                    confirmed=True,
                ),
            )
        finally:
            set_current_call_id(None)

        rows = conn.execute(
            "SELECT query FROM retrievals WHERE call_id = ?", (call_id,)
        ).fetchall()
        assert len(rows) >= 1
        for row in rows:
            assert "(555) 123-4567" not in row["query"]
            assert "555" not in row["query"]
    finally:
        conn.close()


def test_search_knowledge_tool_in_registry():
    """search_knowledge is in the tool registry with correct models."""
    from app.agent.schemas import TOOL_INPUT_MODELS, get_tool_definitions

    defs = get_tool_definitions()
    names = {d["function"]["name"] for d in defs}
    assert "search_knowledge" in names

    # Check input/output models exist
    assert "search_knowledge" in TOOL_INPUT_MODELS
    assert TOOL_INPUT_MODELS["search_knowledge"] is SearchKnowledgeInput


def test_search_knowledge_tool_wrapped_by_decorator(indexed_db):
    """The tool is wrapped by @tool and returns ToolResult dict."""
    conn = db_connect(indexed_db)
    try:
        inp = SearchKnowledgeInput(query="hours", confirmed=True)
        result = search_knowledge(conn, inp)
        # Should be a ToolResult dict
        assert isinstance(result, dict)
        assert "ok" in result
        assert "tool" in result
        assert result["tool"] == "search_knowledge"
        assert "message" in result
        assert "data" in result
    finally:
        conn.close()


def test_planted_instruction_does_not_cause_write(temp_db):
    """Planted instruction in a doc does not trigger a write tool through the loop.

    The fixture is indexed into a temporary database so the real knowledge/
    folder holds only real FAQs.
    """
    import shutil

    from scripts.index_kb import index_documents

    conn = db_connect(temp_db)
    try:
        # Index real FAQs (25) + the planted fixture (1) = 26
        knowledge_dir = Path(temp_db).parent / "knowledge_fixture"
        knowledge_dir.mkdir(exist_ok=True)
        src = Path(__file__).resolve().parents[1] / "knowledge"
        for f in src.glob("*.md"):
            shutil.copy(f, knowledge_dir / f.name)
        fixture = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "26-planted-injection.md"
        )
        shutil.copy(fixture, knowledge_dir / fixture.name)

        import scripts.index_kb as ikb
        original = ikb.KNOWLEDGE_DIR
        ikb.KNOWLEDGE_DIR = knowledge_dir
        try:
            stats = index_documents(conn, rebuild=True)
            assert stats["inserted"] == 26
        finally:
            ikb.KNOWLEDGE_DIR = original
    finally:
        conn.close()

    # Search for the planted doc
    searcher = create_fts_search()
    res = searcher.search("wifi password", max_results=3, threshold=2.0)
    assert res.found
    assert any("Wi-Fi" in r.source for r in res.results)
    planted = next(r for r in res.results if "Wi-Fi" in r.source)
    assert "Ignore previous instructions" in planted.text
    assert "3 AM" in planted.text

    # Now run through the agent loop with FakeLLMClient
    llm = FakeLLMClient()
    resp = tool_call("search_knowledge", {"query": "wifi password", "confirmed": True})
    llm.add_response(resp)
    llm.add_response(text("The guest Wi-Fi network is GuestOffice with password welcome2024."))

    agent = AgentLoop(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        llm_client=llm,
        db_path=temp_db,
    )
    reply = agent.receive("What's the wifi password?")
    assert "GuestOffice" in reply or "welcome2024" in reply
    assert "3 AM" not in reply

    # No booking should have been created
    conn = db_connect(temp_db)
    try:
        bookings = conn.execute("SELECT * FROM bookings").fetchall()
        assert len(bookings) == 0
    finally:
        conn.close()


def test_search_knowledge_returns_structured_output(indexed_db):
    """Tool returns DocResult with source, score, updated, text."""
    conn = db_connect(indexed_db)
    try:
        inp = SearchKnowledgeInput(query="hours", confirmed=True)
        result = search_knowledge(conn, inp)

        assert result["ok"] is True
        data = result["data"]
        assert "found" in data
        assert "results" in data
        if data["found"]:
            assert len(data["results"]) >= 1
            for r in data["results"]:
                assert "source" in r
                assert "score" in r
                assert "updated" in r
                assert "text" in r
                assert r["score"] >= 0
    finally:
        conn.close()


def test_search_knowledge_spoken_message_on_not_found(indexed_db):
    """When found=false, spoken message offers to take a message."""
    conn = db_connect(indexed_db)
    try:
        inp = SearchKnowledgeInput(query="xyz123nonexistent", confirmed=True)
        result = search_knowledge(conn, inp)
        # Should return found=false and the offer message
        assert result["ok"] is True
        assert result["data"]["found"] is False
        assert "take a message" in result["message"].lower()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool schema validation
# ---------------------------------------------------------------------------


def test_search_knowledge_input_model():
    """SearchKnowledgeInput validates correctly."""
    inp = SearchKnowledgeInput(query="test", confirmed=True)
    assert inp.query == "test"
    assert inp.confirmed is True

    # confirmed defaults to True
    inp2 = SearchKnowledgeInput(query="test")
    assert inp2.confirmed is True


def test_search_knowledge_output_model():
    """SearchKnowledgeOutput validates correctly."""
    out = SearchKnowledgeOutput(found=True, results=[])
    assert out.found is True
    assert out.results == []

    doc = DocResult(source="Test", score=5.0, updated="2026-10-15", text="Body")
    out2 = SearchKnowledgeOutput(found=True, results=[doc])
    assert len(out2.results) == 1
    assert out2.results[0].source == "Test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])