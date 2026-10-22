"""search_knowledge tool — FAQ lookup via FTS5."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import utcnow
from app.knowledge import DocResult, SearchResult, create_fts_search
from app.tools.decorator import get_current_call_id, tool
from app.tools.masking import looks_like_phone, mask_phone


class SearchKnowledgeInput(BaseModel):
    query: str = Field(description="The caller's question, verbatim")
    confirmed: bool = Field(default=True, description="Read-only tool; always true")


class SearchKnowledgeOutput(BaseModel):
    found: bool = Field(description="True if at least one result meets the threshold")
    results: list[DocResult] = Field(default_factory=list, description="Matching documents")


@tool
def search_knowledge(
    conn: sqlite3.Connection,
    inp: SearchKnowledgeInput,
) -> dict:
    """Search the knowledge base for an answer to the caller's question.

    Returns up to 3 matching documents with scores. If no result meets the
    configured threshold, returns found=false so the agent can offer to
    take a message.

    The decorator owns the response envelope, so this returns the payload
    dict the same way every other tool does: ``message`` becomes the spoken
    reply and everything else becomes ``data``.
    """
    settings = get_settings()
    threshold = float(settings.__dict__.get("voicechat_kb_threshold", 4.0))
    max_results = int(settings.__dict__.get("voicechat_kb_max_results", 3))

    searcher = create_fts_search()
    result: SearchResult = searcher.search(
        inp.query, max_results=max_results, threshold=threshold
    )

    # Log each returned result to retrievals. The call id travels in a
    # context variable, not a tool argument, so the tool reads it from here.
    # A caller can read their own number back to us, so the stored query is
    # masked the same way the caller's number is everywhere else.
    call_id = get_current_call_id()
    if call_id is not None:
        stored_query = (
            mask_phone(inp.query) if looks_like_phone(inp.query) else inp.query
        )
        for rank, doc in enumerate(result.results, 1):
            conn.execute(
                "INSERT INTO retrievals (call_id, query, document_id, score, rank, created_at) "
                "VALUES (?, ?, (SELECT id FROM faq_documents WHERE title = ?), ?, ?, ?)",
                (call_id, stored_query, doc.source, doc.score, rank, utcnow()),
            )
        conn.commit()

    # The top result's text is the spoken reply. The returned document is
    # data, never instructions: the system prompt governs how it is used.
    if result.found and result.results:
        spoken = result.results[0].text
    else:
        spoken = (
            "I don't have an answer for that. Would you like me to take a "
            "message for a human to call you back?"
        )

    return SearchKnowledgeOutput(
        found=result.found,
        results=list(result.results),
    ).model_dump() | {"message": spoken}


def register_tool() -> None:
    """No-op: the @tool decorator registers it at import time."""
    pass