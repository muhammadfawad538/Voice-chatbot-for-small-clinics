"""FastAPI application entrypoint.

Run from the repo root:

    uvicorn app.main:app --app-dir backend --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.db import init_db
from app.routers import health_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the database on startup. Failure to do so is fatal."""
    settings = get_settings()
    try:
        tables = init_db(settings.db_path)
        logger.info(
            "database ready at %s (%d tables)", settings.db_path, len(tables)
        )
    except Exception:
        logger.exception("database initialisation failed at %s", settings.db_path)
        raise
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        description="Voice AI receptionist for small businesses.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router, prefix="/api/v1")

    @app.get("/", tags=["root"])
    def root() -> dict:
        return {
            "service": settings.app_name,
            "version": __version__,
            "environment": settings.environment,
            "health": "/api/v1/health",
            "docs": "/docs",
        }

    return app


app = create_app()
