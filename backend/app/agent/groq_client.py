"""Groq tool-calling client wrapper.

Swappable interface so a different provider can be plugged in later.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from groq import Groq

from app.config import get_settings


@dataclass
class LLMUsage:
    """Token usage from a single completion."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str


@dataclass
class LLMResponse:
    """Structured response from the LLM."""
    content: str | None
    tool_calls: list[dict[str, Any]]
    usage: LLMUsage | None
    raw: Any


class GroqClient:
    """Wrapper around Groq's tool-calling chat completion."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
        timeout: float = 30.0,
    ):
        settings = get_settings()
        self._api_key = (
            api_key
            or os.environ.get("GROQ_API_KEY")
            or settings.__dict__.get("groq_api_key")
        )
        self._model = model or settings.__dict__.get("groq_model", "llama-3.3-70b-versatile")
        self._max_retries = max_retries
        self._timeout = timeout
        self._client: Groq | None = None

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> Groq:
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("GROQ_API_KEY not set")
            self._client = Groq(api_key=self._api_key, timeout=self._timeout)
        return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        """Single chat completion with tool calling.

        Retries on transient errors with exponential backoff.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                client = self._get_client()
                resp = client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=0.1,
                    max_tokens=1024,
                )
                msg = resp.choices[0].message
                tool_calls = []
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        tool_calls.append({
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        })
                usage = None
                if resp.usage:
                    usage = LLMUsage(
                        prompt_tokens=resp.usage.prompt_tokens,
                        completion_tokens=resp.usage.completion_tokens,
                        total_tokens=resp.usage.total_tokens,
                        model=self._model,
                    )
                return LLMResponse(
                    content=msg.content,
                    tool_calls=tool_calls,
                    usage=usage,
                    raw=resp,
                )
            except Exception as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s
                    continue
                raise

        raise last_exc  # type: ignore


class FakeLLMClient:
    """Deterministic fake LLM for unit tests (no API key needed)."""

    def __init__(self, responses: list[LLMResponse] | None = None):
        self._responses = responses or []
        self._call_count = 0

    def add_response(self, response: LLMResponse) -> None:
        self._responses.append(response)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
        else:
            # Default: polite handoff
            resp = LLMResponse(
                content="I'm transferring you to a human. Please hold.",
                tool_calls=[{
                    "id": "fake_handoff_1",
                    "name": "handoff_to_human",
                    "arguments": json.dumps({
                        "reason": "unsure",
                        "summary": "Fake LLM fallback",
                        "confirmed": True,
                    }),
                }],
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0, model="fake"),
                raw=None,
            )
        self._call_count += 1
        return resp

    @property
    def model(self) -> str:
        return "fake"


def create_llm_client() -> GroqClient | FakeLLMClient:
    """Factory: real client in prod, fake in tests (when GROQ_API_KEY absent)."""
    if os.environ.get("GROQ_API_KEY") or get_settings().__dict__.get("groq_api_key"):
        return GroqClient()
    return FakeLLMClient()


__all__ = [
    "GroqClient",
    "FakeLLMClient",
    "LLMResponse",
    "LLMUsage",
    "create_llm_client",
]