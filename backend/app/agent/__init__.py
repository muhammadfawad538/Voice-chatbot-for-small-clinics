"""Agent module for Phase 2b — Text Agent Loop.

Public API:
- AgentLoop: main conversation driver
- AgentConfig: configuration
- run_conversation: convenience for testing
- GroqClient, FakeLLMClient: LLM clients
- get_tool_definitions, validate_tool_arguments: tool schemas
"""

from __future__ import annotations

from .groq_client import FakeLLMClient, GroqClient, LLMResponse, LLMUsage, create_llm_client
from .loop import (
    AgentConfig,
    AgentLoop,
    CallState,
    execute_tool,
    load_system_prompt,
    run_conversation,
)
from .schemas import get_tool_definitions, get_tool_map, validate_tool_arguments

__all__ = [
    # Loop
    "AgentLoop",
    "AgentConfig",
    "CallState",
    "run_conversation",
    "load_system_prompt",
    "execute_tool",
    # LLM clients
    "GroqClient",
    "FakeLLMClient",
    "LLMResponse",
    "LLMUsage",
    "create_llm_client",
    # Schemas
    "get_tool_definitions",
    "get_tool_map",
    "validate_tool_arguments",
]