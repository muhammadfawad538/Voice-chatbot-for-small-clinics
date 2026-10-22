"""Tool JSON schema generation from Pydantic models.

Single source of truth for the LLM tool definitions — no manual duplication.

The schemas are derived from the Phase 2a input models, so a change to a tool's
contract shows up here automatically. ``confirmed`` stays in the schema on
purpose: the LLM has to make that choice explicitly, and it is the tripwire
that makes "the agent confirmed" an observable event.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.tools import (
    BookSlotInput,
    CancelInput,
    CheckAvailabilityInput,
    HandoffToHumanInput,
    RescheduleInput,
    SearchKnowledgeInput,
)

# Fields the LLM must never see or set.
INTERNAL_FIELDS: frozenset[str] = frozenset({"failure_mode", "injected"})

# Fields the LLM must always make an explicit choice about.
EXPLICIT_FIELDS: frozenset[str] = frozenset({"confirmed"})

# Tool name -> input model. The single registry the loop, the schema builder
# and the validator all read from.
TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "check_availability": CheckAvailabilityInput,
    "book_slot": BookSlotInput,
    "reschedule": RescheduleInput,
    "cancel": CancelInput,
    "handoff_to_human": HandoffToHumanInput,
    "search_knowledge": SearchKnowledgeInput,
}

# Tools that write. Their `confirmed` flag is enforced in code.
WRITE_TOOLS: frozenset[str] = frozenset(
    {"book_slot", "reschedule", "cancel", "handoff_to_human"}
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "check_availability": (
        "Check free appointment slots for a given business date. "
        "Returns a list of HH:MM times in the business timezone."
    ),
    "book_slot": (
        "Book an appointment slot for the caller. Call this ONLY after reading "
        "back the caller's name, date, and time and receiving explicit "
        "confirmation from the caller. Set confirmed=true only then."
    ),
    "reschedule": (
        "Move an existing appointment to a new slot. Call this ONLY after "
        "reading back the old and new details and receiving explicit "
        "confirmation from the caller. Set confirmed=true only then."
    ),
    "cancel": (
        "Cancel an existing appointment. Call this ONLY after reading back the "
        "details and receiving explicit confirmation from the caller. "
        "Set confirmed=true only then."
    ),
    "handoff_to_human": (
        "Transfer the caller to a human staff member. Use for complaints, "
        "urgent or sensitive requests, medical questions, or when you are "
        "unsure. Set confirmed=true to perform the transfer."
    ),
    "search_knowledge": (
        "Search the knowledge base for an answer to the caller's question. "
        "Returns up to 3 matching documents with scores. If no result meets "
        "the threshold, returns found=false."
    ),
}


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------


def _build_parameters(model: type[BaseModel]) -> dict[str, Any]:
    """Build an OpenAI-style parameters object from a Pydantic input model."""
    schema = model.model_json_schema()

    properties: dict[str, Any] = dict(schema.get("properties", {}))
    required: list[str] = list(schema.get("required", []))

    # Drop internal fields from both the properties and the required list.
    for internal in INTERNAL_FIELDS:
        properties.pop(internal, None)
        required = [r for r in required if r != internal]

    # `confirmed` must be an explicit choice, not a silent default.
    for explicit in EXPLICIT_FIELDS:
        if explicit in properties and explicit not in required:
            required.append(explicit)

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    # Pydantic puts nested/enum definitions under $defs — keep them so the
    # schema stays self-contained.
    if "$defs" in schema:
        parameters["$defs"] = schema["$defs"]
    return parameters


def _tool_schema(name: str) -> dict[str, Any]:
    model = TOOL_INPUT_MODELS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "parameters": _build_parameters(model),
        },
    }


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return the tool definitions for the LLM."""
    return [_tool_schema(name) for name in TOOL_INPUT_MODELS]


# Tool name -> input model class name for validation.
TOOL_MODEL_NAMES: dict[str, str] = {
    "check_availability": "CheckAvailabilityInput",
    "book_slot": "BookSlotInput",
    "reschedule": "RescheduleInput",
    "cancel": "CancelInput",
    "handoff_to_human": "HandoffToHumanInput",
    "search_knowledge": "SearchKnowledgeInput",
}


def get_tool_map() -> dict[str, str]:
    """Map tool name -> input model class name for validation."""
    return TOOL_MODEL_NAMES


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_tool_arguments(
    tool_name: str, arguments: dict[str, Any]
) -> tuple[bool, str | None, BaseModel | None]:
    """Validate tool arguments against the input model.

    Returns ``(is_valid, error_message, parsed_model)``. The parsed model is
    ``None`` when validation fails. This runs before the tool is executed, so
    a malformed call never reaches the calendar.
    """
    model = TOOL_INPUT_MODELS.get(tool_name)
    if model is None:
        return False, f"Unknown tool: {tool_name}", None

    if not isinstance(arguments, dict):
        return False, "Tool arguments must be an object", None

    try:
        parsed = model(**arguments)
    except Exception as exc:
        return False, f"Invalid arguments for {tool_name}: {exc}", None

    # The write tripwire: the model must assert that the caller agreed.
    if tool_name in WRITE_TOOLS and not getattr(parsed, "confirmed", False):
        return False, f"{tool_name} requires confirmed=true", None

    return True, None, parsed


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------


def print_tool_schemas() -> None:
    """Pretty-print all tool schemas for inspection."""
    print(json.dumps(get_tool_definitions(), indent=2))


__all__ = [
    "get_tool_definitions",
    "validate_tool_arguments",
    "TOOL_INPUT_MODELS",
    "WRITE_TOOLS",
]
