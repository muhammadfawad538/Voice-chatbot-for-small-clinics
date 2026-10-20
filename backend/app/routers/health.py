"""Health check.

Verifies the database by checking the expected table set exactly, so a schema
that failed to initialise or drifted from schema.sql is caught immediately.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from app import __version__, db
from app.config import get_settings
from app.models import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse | JSONResponse:
    """Report service and database status.

    200 when the schema is exactly as expected. 503 otherwise, with the reason
    logged server-side but never returned in the body.
    """
    settings = get_settings()
    try:
        # Accessed through the module so tests can substitute the database.
        conn = db.connect(settings.db_path)
        try:
            healthy, problems = db.check_schema(conn)
        finally:
            conn.close()
    except Exception:
        # Logged, not returned — the body must not leak exception text.
        logger.exception("health check: database unavailable")
        healthy, problems = False, ["database unavailable"]

    if not healthy:
        logger.error("health check failed: %s", "; ".join(problems))
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "version": __version__,
                "database": "error",
                "timestamp": db.utcnow(),
            },
        )

    return HealthResponse(
        status="ok",
        version=__version__,
        database="ok",
        timestamp=db.utcnow(),
    )
