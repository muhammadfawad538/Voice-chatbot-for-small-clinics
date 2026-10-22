"""Request/response models for the voice endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceTurnRequest(BaseModel):
    """Input for a single voice turn."""

    session_id: str = Field(
        min_length=1, max_length=64, description="Opaque session identifier"
    )
    transcript: str = Field(
        min_length=1, max_length=4096, description="Caller transcript from browser STT"
    )
    timings: dict[str, int] | None = Field(
        default=None,
        description="Browser-side timings in ms: {stt, synthesis, network}",
    )


class VoiceEndRequest(BaseModel):
    """Input for closing a voice session. The caller hung up — no new text."""

    session_id: str = Field(
        min_length=1, max_length=64, description="Opaque session identifier"
    )


class VoiceTurnResponse(BaseModel):
    """Output for a single voice turn."""

    reply: str = Field(description="Bot text to speak via browser TTS")
    call_id: int | None = Field(default=None, description="Call identifier, if a call row exists")
    ended: bool = Field(default=False, description="True when the call was handed off or closed")
    timings: dict[str, int] = Field(
        default_factory=dict, description="Server timings in ms: {agent_ms, total_ms}"
    )
