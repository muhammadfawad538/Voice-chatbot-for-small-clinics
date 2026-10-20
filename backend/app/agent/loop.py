"""Agent loop: receive → think → call tool → respond.

Synchronous, single-threaded, one call per instance.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import connect as db_connect
from app.db import utcnow
from app.tools import (
    book_slot,
    cancel,
    check_availability,
    handoff_to_human,
    reschedule,
)
from app.tools.decorator import ToolResult
from app.tools.tools import (
    BookSlotInput,
    CancelInput,
    CheckAvailabilityInput,
    HandoffToHumanInput,
    RescheduleInput,
)

from .groq_client import FakeLLMClient, GroqClient, create_llm_client
from .schemas import get_tool_definitions

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    max_steps: int = 10
    call_timeout_s: float = 30.0
    system_prompt_path: Path | None = None

    @classmethod
    def from_env(cls) -> AgentConfig:
        settings = get_settings()
        return cls(
            max_steps=int(settings.__dict__.get("voicechat_max_steps", 10)),
            call_timeout_s=float(settings.__dict__.get("voicechat_call_timeout_s", 30.0)),
            system_prompt_path=(
                Path(settings.__dict__["voicechat_system_prompt_path"])
                if settings.__dict__.get("voicechat_system_prompt_path")
                else None
            ),
        )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class CallState:
    """Mutable state for a single call."""
    call_id: int | None = None
    caller_number_masked: str | None = None
    started_at: str = field(default_factory=utcnow)
    step_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    handoff_triggered: bool = False
    ended: bool = False


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


TOOL_FUNCTIONS = {
    "check_availability": (check_availability, CheckAvailabilityInput),
    "book_slot": (book_slot, BookSlotInput),
    "reschedule": (reschedule, RescheduleInput),
    "cancel": (cancel, CancelInput),
    "handoff_to_human": (handoff_to_human, HandoffToHumanInput),
}


def execute_tool(
    conn: sqlite3.Connection,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: int | None = None,
) -> dict[str, Any]:
    """Execute a tool with validated arguments.

    The decorator handles logging, masking, and error formatting. The call_id
    is passed through a context variable so it reaches tools that need it
    (handoff_to_human). Validation failures are logged here so every tool
    invocation leaves an audit trail.
    """
    import json

    from pydantic import ValidationError

    from app.db import log_tool_call
    from app.tools.decorator import _mask_args, set_current_call_id

    func, input_model = TOOL_FUNCTIONS[tool_name]
    try:
        inp = input_model(**arguments)
    except ValidationError as exc:
        # Validation failed — build the error result and log it so the attempt
        # is visible in the audit trail even though the tool body never ran.
        tr = ToolResult(
            ok=False,
            tool=tool_name,
            error_code="invalid_arguments",
            message="That request doesn't look right. Let me double-check the details.",
            data={"detail": str(exc)},
        )
        log_tool_call(
            conn,
            call_id=call_id,
            tool_name=tool_name,
            arguments_json=_mask_args(((),), {"arguments": arguments}),
            result_json=json.dumps(tr.to_dict(), separators=(",", ":"), default=str),
            success=False,
            latency_ms=0,
            injected=False,
        )
        return tr.to_dict()

    set_current_call_id(call_id)
    try:
        return func(conn, inp)
    finally:
        # Clear the context var after the tool call so it doesn't leak into
        # the next call. A nested decorator call keeps whatever it set, so
        # the next tool does not see a stale value.
        set_current_call_id(None)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


DEFAULT_SYSTEM_PROMPT = Path(__file__).with_name("prompts") / "system.md"


def load_system_prompt(path: Path | None = None) -> str:
    """Load the system prompt from a markdown file."""
    p = path or DEFAULT_SYSTEM_PROMPT
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


class AgentLoop:
    """One conversation loop per call."""

    def __init__(
        self,
        caller_number: str,
        caller_number_masked: str,
        config: AgentConfig | None = None,
        llm_client: GroqClient | FakeLLMClient | None = None,
        db_path: str | Path | None = None,
    ):
        self.caller_number = caller_number
        self.caller_number_masked = caller_number_masked
        self.config = config or AgentConfig.from_env()
        self.llm = llm_client or create_llm_client()
        self.db_path = db_path
        self.state = CallState()
        self._start_time = time.monotonic()
        self._conn: sqlite3.Connection | None = None

        # Load system prompt once
        self._system_prompt = load_system_prompt(self.config.system_prompt_path)

    @contextmanager
    def _db(self):
        """Provide a database connection."""
        if self._conn is not None:
            yield self._conn
        else:
            conn = db_connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

    def _ensure_call_row(self) -> int:
        """Create the calls row on first turn."""
        if self.state.call_id is not None:
            return self.state.call_id
        with self._db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO calls (caller_number_masked, status, started_at)
                VALUES (?, 'active', ?)
                """,
                (self.caller_number_masked, utcnow()),
            )
            conn.commit()
            self.state.call_id = cursor.lastrowid
        return self.state.call_id

    def _log_turn(self, speaker: str, text: str, latency_ms: int | None = None) -> None:
        """Write a call_turns row."""
        if self.state.call_id is None:
            return
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO call_turns (call_id, speaker, text, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.state.call_id, speaker, text, latency_ms, utcnow()),
            )
            conn.commit()

    def _log_cost(self) -> None:
        """Log accumulated token usage to eval_results."""
        if self.state.call_id is None:
            return
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO eval_results (eval_name, case_id, passed, details_json, created_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (
                    "cost",
                    str(self.state.call_id),
                    json.dumps({
                        "prompt_tokens": self.state.total_prompt_tokens,
                        "completion_tokens": self.state.total_completion_tokens,
                        "model": self.llm.model,
                    }),
                    utcnow(),
                ),
            )
            conn.commit()

    def _check_timeout(self) -> bool:
        """Return True if the call has exceeded its wall-clock timeout."""
        return (time.monotonic() - self._start_time) > self.config.call_timeout_s

    def _build_messages(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build the message list for the LLM: system + history."""
        return [
            {"role": "system", "content": self._system_prompt},
            *history,
        ]

    def receive(self, user_text: str) -> str:
        """Process one caller utterance and return the bot's reply.

        Raises RuntimeError if the call has ended or timed out.
        """
        if self.state.ended:
            raise RuntimeError("Call already ended")
        if self._check_timeout():
            return self._force_handoff("Call timeout — transferring to a human.")

        # Ensure call row exists FIRST so the caller turn is logged with call_id
        call_id = self._ensure_call_row()

        # Log caller turn
        turn_start = time.monotonic()
        self._log_turn("caller", user_text)

        # Build conversation history (caller + bot turns so far)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT speaker, text FROM call_turns WHERE call_id = ? ORDER BY id",
                (call_id,),
            ).fetchall()

        history = [
            {"role": "user" if r["speaker"] == "caller" else "assistant", "content": r["text"]}
            for r in rows
        ]

        # Add the new user message
        history.append({"role": "user", "content": user_text})

        # LLM loop: think → (maybe call tool) → respond
        messages = self._build_messages(history)
        tools = get_tool_definitions()

        for _ in range(self.config.max_steps):
            self.state.step_count += 1

            if self._check_timeout():
                return self._force_handoff("Call timeout — transferring to a human.")

            # Get LLM response
            resp = self.llm.chat(messages, tools)

            # Accumulate usage
            if resp.usage:
                self.state.total_prompt_tokens += resp.usage.prompt_tokens
                self.state.total_completion_tokens += resp.usage.completion_tokens

            # If the LLM wants to call a tool
            if resp.tool_calls:
                for tc in resp.tool_calls:
                    tool_name = tc["name"]

                    if tool_name not in TOOL_FUNCTIONS:
                        # An unknown tool never reaches the database, but the
                        # attempt is still worth logging.
                        tool_result = {
                            "ok": False,
                            "tool": tool_name,
                            "error_code": "invalid_arguments",
                            "message": (
                                "That request doesn't look right. "
                                "Let me double-check the details."
                            ),
                            "data": {},
                        }
                    else:
                        # The decorator validates the arguments against the
                        # input model and refuses to run the tool body if they
                        # don't fit. Both a malformed call and a refused write
                        # are logged, so a steered tool call leaves a trail.
                        try:
                            args = json.loads(tc["arguments"])
                        except json.JSONDecodeError:
                            args = None

                        if args is None:
                            tool_result = {
                                "ok": False,
                                "tool": tool_name,
                                "error_code": "invalid_arguments",
                                "message": (
                                    "That request doesn't look right. "
                                    "Let me double-check the details."
                                ),
                                "data": {},
                            }
                        else:
                            with self._db() as conn:
                                tool_result = execute_tool(conn, tool_name, args, call_id)

                    # Feed tool result back to LLM
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tool_name,
                            "content": json.dumps(tool_result),
                        }
                    )

                    # Check for handoff
                    if tool_name == "handoff_to_human" and tool_result.get("ok"):
                        self.state.handoff_triggered = True
                        reply = tool_result.get(
                            "message", "I'm transferring you to a human. Please hold."
                        )
                        self._log_turn(
                            "bot", reply, int((time.monotonic() - turn_start) * 1000)
                        )
                        self.end_call()
                        return reply

                # Continue the loop to let LLM respond to tool results
                continue

            # No tool calls → final response
            reply = (
                resp.content
                or "I'm not sure how to help with that. Let me transfer you to a human."
            )
            self._log_turn(
                "bot", reply, int((time.monotonic() - turn_start) * 1000)
            )
            return reply

        # Step limit exceeded
        return self._force_handoff("I'm not sure how to proceed. Let me transfer you to a human.")

    def _force_handoff(self, message: str) -> str:
        """Trigger handoff and end the call."""
        if not self.state.handoff_triggered and self.state.call_id is not None:
            with self._db() as conn:
                execute_tool(
                    conn,
                    "handoff_to_human",
                    {
                        "reason": "unsure",
                        "summary": "Step limit or timeout exceeded",
                        "confirmed": True,
                    },
                    self.state.call_id,
                )
            self.state.handoff_triggered = True
        self.end_call()
        return message

    def end_call(self) -> None:
        """Finalise the call: update status, log cost."""
        if self.state.ended:
            return
        self.state.ended = True
        if self.state.call_id is not None:
            status = "handoff" if self.state.handoff_triggered else "completed"
            with self._db() as conn:
                conn.execute(
                    "UPDATE calls SET status = ?, ended_at = ? WHERE id = ?",
                    (status, utcnow(), self.state.call_id),
                )
                conn.commit()
            self._log_cost()

    def get_call_id(self) -> int | None:
        return self.state.call_id


# ---------------------------------------------------------------------------
# Convenience function for simple use
# ---------------------------------------------------------------------------


def run_conversation(
    caller_number: str,
    caller_number_masked: str,
    user_inputs: list[str],
    config: AgentConfig | None = None,
    llm_client: GroqClient | FakeLLMClient | None = None,
    db_path: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Run a full conversation for testing.

    Returns list of (speaker, text) pairs.
    """
    agent = AgentLoop(
        caller_number=caller_number,
        caller_number_masked=caller_number_masked,
        config=config,
        llm_client=llm_client,
        db_path=db_path,
    )
    transcript = []
    for user_text in user_inputs:
        bot_reply = agent.receive(user_text)
        transcript.append(("caller", user_text))
        transcript.append(("bot", bot_reply))
        if agent.state.ended:
            break
    # The caller hung up. Finalise the call so status and cost are recorded.
    agent.end_call()
    return transcript


__all__ = [
    "AgentLoop",
    "AgentConfig",
    "CallState",
    "run_conversation",
    "load_system_prompt",
    "execute_tool",
]