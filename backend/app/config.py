"""Application settings.

Single home for configuration. Everything is overridable through the
environment; see .env.example for the list of supported variables.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root — backend/app/config.py -> backend/app -> backend -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# schema.sql is the single source of truth for the database layout.
SCHEMA_PATH = REPO_ROOT / "schema.sql"

# Default SQLite location. Overridable through DATABASE_URL.
DEFAULT_DB_PATH = REPO_ROOT / "data" / "voice-chatbot.db"


class Settings(BaseSettings):
    """Runtime configuration, loaded from the environment and .env."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        default="sqlite:///./data/voice-chatbot.db",
        description="SQLite path. Accepts a raw path or a sqlite:/// URL.",
    )
    app_name: str = "voice-chatbot"
    app_version: str = "0.1.0"
    environment: str = "development"
    cors_origins: str = "http://localhost:3000"

    # Calendar configuration
    calendar_timezone: str = Field(
        default="America/New_York",
        description="IANA timezone for the business (e.g. America/New_York)",
    )
    calendar_open_time: str = Field(
        default="09:00",
        description="Opening time in HH:MM format, business-local",
    )
    calendar_close_time: str = Field(
        default="17:00",
        description="Closing time in HH:MM format, business-local",
    )
    calendar_slot_minutes: int = Field(
        default=30,
        description="Length of a booking slot in minutes",
    )
    calendar_closed_weekdays: str = Field(
        default="saturday,sunday",
        description="Comma-separated weekday names when the business is closed",
    )

    # Failure injection — for manual demos and tests only.
    # The app refuses to start if these are set outside dev/test.
    calendar_failure_mode: str | None = Field(
        default=None,
        description=(
            "Failure mode for the mock calendar: timeout, server_error, "
            "slot_taken_at_last_moment. Dev/test only."
        ),
    )
    calendar_failure_times: int | None = Field(
        default=None,
        description="How many calls fail. Unset means always. Dev/test only.",
    )

    # Phase 2b — Agent Loop
    groq_api_key: str | None = Field(
        default=None,
        description="Groq API key for tool-calling LLM.",
    )
    groq_model: str = Field(
        default="llama-3.3-70b-versatile",
        description="Groq model name for the agent.",
    )
    voicechat_max_steps: int = Field(
        default=10,
        description="Maximum conversation turns per call.",
    )
    voicechat_call_timeout_s: float = Field(
        default=30.0,
        description="Per-call wall-clock timeout in seconds.",
    )
    voicechat_system_prompt_path: str | None = Field(
        default=None,
        description="Optional path to override the built-in system prompt.",
    )

    @property
    def db_path(self) -> Path:
        """Resolve DATABASE_URL to an absolute filesystem path."""
        url = self.database_url.strip()
        prefix = "sqlite:///"
        if url.startswith(prefix):
            url = url[len(prefix) :]
        path = Path(url)
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()

    @property
    def schema_path(self) -> Path:
        return SCHEMA_PATH

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance. Tests override this rather than the env."""
    return Settings()


settings = get_settings()
