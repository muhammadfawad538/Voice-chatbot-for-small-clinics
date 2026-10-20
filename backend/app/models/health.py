"""Health response contract."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Shape of GET /api/v1/health.

    Deliberately carries no raw exception text: the failure detail stays in the
    server log, the response says only that the database is not usable.
    """

    status: str = Field(description="ok when the service and database are usable")
    version: str = Field(description="service version")
    database: str = Field(description="ok or error")
    timestamp: str = Field(description="UTC ISO-8601")

    model_config = {"json_schema_extra": {"example": {
        "status": "ok",
        "version": "0.1.0",
        "database": "ok",
        "timestamp": "2026-10-19T12:00:00+00:00",
    }}}
