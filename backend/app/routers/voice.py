"""Voice endpoints: browser transcript → agent loop → reply.

Audio never reaches the server — only the transcript text and per-stage timings.
The existing AgentLoop is reused, so validation, tool confirmation and handoff
rules apply identically to the text path.
"""

from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, HTTPException

from app.agent import AgentConfig, AgentLoop, create_llm_client
from app.config import settings as cached_settings
from app.models.voice import VoiceEndRequest, VoiceTurnRequest, VoiceTurnResponse
from app.tools.masking import mask_phone

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

#: Maximum transcript size in bytes. A spoken turn is a sentence or two;
#: anything larger is a client bug or an attempt to flood the agent.
MAX_TRANSCRIPT_BYTES = 2000

#: Maximum number of concurrent voice sessions kept in memory.
MAX_SESSIONS = 64

#: One AgentLoop per voice session. v1 keeps these in process memory; a
#: multi-worker deployment would move this to a shared store.
_sessions: dict[str, AgentLoop] = {}

#: Phone numbers as they arrive in speech: (555) 123-4567, 555-123-4567,
#: 555.123.4567, or ten bare digits.
_PHONE = re.compile(
    r"(?:\(\d{3}\)\s?\d{3}-\d{4}"
    r"|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
    r"|\b\d{10}\b)"
)


def _mask_transcript(text: str) -> str:
    """Mask phone numbers inside free-text transcript.

    Speech transcripts are far more likely to contain a spoken number than
    typed text, so the voice path masks before the transcript reaches the
    agent — which means the masked form is what gets stored.
    """
    return _PHONE.sub(lambda m: mask_phone(m.group(0)), text)


def _make_agent(session_id: str) -> AgentLoop:
    """Build a fresh AgentLoop for a voice session.

    Separated from the session lookup so tests can substitute an agent
    backed by a fake LLM.
    """
    config = AgentConfig.from_env()
    return AgentLoop(
        caller_number="(555) 123-4567",
        caller_number_masked="(XXX) XXX-4567",
        config=config,
        llm_client=create_llm_client(),
        db_path=cached_settings.db_path,
    )


def _get_or_create_agent(session_id: str) -> AgentLoop:
    """Return the AgentLoop for a session, creating it on first use."""
    agent = _sessions.get(session_id)
    if agent is None:
        if len(_sessions) >= MAX_SESSIONS:
            # Evict the oldest session rather than growing without bound.
            oldest = next(iter(_sessions))
            _sessions.pop(oldest, None)
            logger.warning("voice session limit reached; evicted session %s", oldest)
        agent = _make_agent(session_id)
        _sessions[session_id] = agent
    return agent


@router.post("/voice/turn", response_model=VoiceTurnResponse)
async def voice_turn(payload: VoiceTurnRequest):
    """Process one voice turn from the browser.

    The transcript was produced by the browser's SpeechRecognition. We run the
    AgentLoop with it as the caller's utterance and return the bot's reply
    plus per-stage timings.

    Audio never reaches the server.
    """
    if not payload.transcript.strip():
        raise HTTPException(status_code=400, detail="Empty transcript")

    transcript_bytes = len(payload.transcript.encode("utf-8"))
    if transcript_bytes > MAX_TRANSCRIPT_BYTES:
        logger.warning("voice turn: transcript oversized (%d bytes)", transcript_bytes)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Transcript too large ({transcript_bytes} bytes, "
                f"max {MAX_TRANSCRIPT_BYTES}). Please speak shorter."
            ),
        )

    agent = _get_or_create_agent(payload.session_id)
    masked_transcript = _mask_transcript(payload.transcript)

    agent_start = time.monotonic_ns() // 1_000_000
    try:
        reply = agent.receive(masked_transcript)
    except Exception:
        logger.exception("voice turn: agent.receive failed")
        reply = (
            "I'm sorry, I had trouble understanding that. "
            "Let me transfer you to a human."
        )
    agent_ms = (time.monotonic_ns() // 1_000_000) - agent_start

    # The agent ends the call itself on handoff. Otherwise the session stays
    # open for the next turn; /voice/end closes it.
    ended = agent.state.ended

    client_timings = payload.timings or {}
    return VoiceTurnResponse(
        reply=reply,
        call_id=agent.state.call_id,
        ended=ended,
        timings={
            "agent_ms": agent_ms,
            "total_ms": agent_ms,
            "stt_ms": client_timings.get("stt", 0),
            "synthesis_ms": client_timings.get("synthesis", 0),
            "network_ms": client_timings.get("network", 0),
        },
    )


@router.post("/voice/end", response_model=VoiceTurnResponse)
async def voice_end(payload: VoiceEndRequest):
    """Close a voice session: finalise the call, log cost, drop the session.

    The transcript is optional here — the caller hangs up, so there is nothing
    new to process.
    """
    agent = _sessions.pop(payload.session_id, None)
    if agent is None:
        return VoiceTurnResponse(
            reply="",
            call_id=None,
            ended=True,
            timings={"agent_ms": 0, "total_ms": 0},
        )

    agent.end_call()
    return VoiceTurnResponse(
        reply="",
        call_id=agent.state.call_id,
        ended=True,
        timings={"agent_ms": 0, "total_ms": 0},
    )
