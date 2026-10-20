"""Pydantic response models.

Typed contracts for what the API returns. Request models arrive with the
agent tools in Phase 2.
"""

from .health import HealthResponse

__all__ = ["HealthResponse"]
