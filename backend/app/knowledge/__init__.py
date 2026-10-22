"""Knowledge base package."""

from __future__ import annotations

from .fts_search import FtsKnowledgeSearch, create_fts_search
from .interface import DocResult, KnowledgeSearch, KnowledgeSearchBase, SearchResult

__all__ = [
    "DocResult",
    "FtsKnowledgeSearch",
    "KnowledgeSearch",
    "KnowledgeSearchBase",
    "SearchResult",
    "create_fts_search",
]