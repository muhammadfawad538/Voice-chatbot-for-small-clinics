"""Knowledge search interface — swappable backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DocResult:
    """One retrieved document."""
    source: str        # document title
    score: float       # raw score (BM25 for FTS)
    updated: str       # ISO date from front matter
    text: str          # document body


@dataclass(frozen=True)
class SearchResult:
    """Result of a knowledge search."""
    found: bool
    results: tuple[DocResult, ...]


class KnowledgeSearch(Protocol):
    """Protocol for knowledge backends."""

    def search(self, query: str, max_results: int = 3, threshold: float = 4.0) -> SearchResult:
        """Search the knowledge base.

        Args:
            query: Raw caller question.
            max_results: Maximum documents to return.
            threshold: Minimum score for found=True. Below this → found=False.

        Returns:
            SearchResult with found flag and up to max_results documents.
        """
        ...


class KnowledgeSearchBase(ABC):
    """Abstract base class with common logic."""

    @abstractmethod
    def _raw_search(self, query: str, limit: int) -> list[DocResult]:
        """Backend-specific search. Returns all candidates."""
        ...

    def search(self, query: str, max_results: int = 3, threshold: float = 4.0) -> SearchResult:
        candidates = self._raw_search(query, max_results)
        if not candidates:
            return SearchResult(found=False, results=())
        # The top result determines whether we "found" something.
        # BM25 scores are positive (converted from SQLite's negative values).
        # A low top score means the query didn't strongly match any doc.
        top_score = candidates[0].score
        if top_score < threshold:
            return SearchResult(found=False, results=tuple(candidates[:max_results]))
        return SearchResult(found=True, results=tuple(candidates[:max_results]))