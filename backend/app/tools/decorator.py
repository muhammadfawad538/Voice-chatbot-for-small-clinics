"""Tool decorator and logging for Phase 2a.

The ``tool`` decorator wraps a tool function so that it:
- times the call
- logs to ``tool_calls`` with masked phone numbers
- turns any exception into a clean error result

A new tool cannot forget to log, because logging is not in the tool body.
"""

from __future__ import annotations

import json
import sqlite3
import time
import typing
from contextvars import ContextVar
from functools import wraps
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.db import connect as db_connect
from app.db import log_tool_call
from app.tools.calendar import CalendarError
from app.tools.masking import looks_like_phone, mask_phone

# The call id the current tool call belongs to. The agent loop sets it; tools
# that write a call-scoped row (handoff_to_human) read it from here. A context
# var keeps it out of every tool signature.
_current_call_id: ContextVar[int | None] = ContextVar("current_call_id", default=None)


def set_current_call_id(call_id: int | None) -> None:
    """Set the call id for tool calls made in this context."""
    _current_call_id.set(call_id)


def get_current_call_id() -> int | None:
    """The call id for the current tool call, if any."""
    return _current_call_id.get()


# ---------------------------------------------------------------------------
# Tool result
# ---------------------------------------------------------------------------


class ToolResult(BaseModel):
    """Standardised result returned by every tool."""

    ok: bool = Field(description="True when the operation completed successfully")
    tool: str = Field(default="", description="Name of the tool that produced this result")
    error_code: str | None = Field(
        default=None,
        description=(
            "One of: invalid_arguments, confirmation_required, timeout, "
            "provider_error, slot_unavailable, not_found, internal_error"
        ),
    )
    message: str = Field(
        default="",
        description="Human-friendly message; never carries exception text",
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool-specific payload on success",
    )
    injected: bool = Field(
        default=False,
        description="True when the call was an injected failure",
    )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Helper: masked args JSON
# ---------------------------------------------------------------------------


def _mask_value(val: Any) -> Any:
    if isinstance(val, BaseModel):
        return _mask_value(val.model_dump())
    if isinstance(val, str):
        return mask_phone(val) if looks_like_phone(val) else val
    if isinstance(val, dict):
        return {k: _mask_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_mask_value(v) for v in val]
    return val


def _mask_args(args: tuple, kwargs: dict[str, Any]) -> str:
    """Return JSON args with phone numbers masked.

    The first argument (the connection) is always excluded.
    """
    combined: list[Any] = list(args[1:])
    if kwargs:
        combined.append(kwargs)
    return json.dumps(_mask_value(combined), separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# Tool decorator
# ---------------------------------------------------------------------------


def _validate_input(func, args: tuple, kwargs: dict[str, Any]) -> BaseModel | None:
    """Extract and validate the tool's Pydantic input model.

    The second positional parameter of every tool is its input model. The
    decorator validates it here, once, so a malformed call never reaches the
    tool body. Returns ``None`` only if the function has no model parameter.
    """
    import inspect

    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = getattr(func, "__annotations__", {})

    params = list(inspect.signature(func).parameters)
    if len(params) < 2:
        return None

    input_name = params[1]
    model_cls = hints.get(input_name)
    if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
        return None

    if len(args) > 1:
        payload = args[1]
        if isinstance(payload, model_cls):
            return payload
        if isinstance(payload, dict):
            merged = dict(payload)
            merged.update(kwargs)
            return model_cls(**merged)
        return payload

    if input_name in kwargs:
        payload = kwargs.pop(input_name)
        if isinstance(payload, model_cls):
            return payload
        merged = dict(payload or {})
        merged.update(kwargs)
        return model_cls(**merged)

    return model_cls(**kwargs)


def _error_code_from_exception(exc: CalendarError) -> str:
    """Map a calendar exception to its tool error code."""
    if isinstance(exc, CalendarError):
        name = type(exc).__name__
        if "Timeout" in name:
            return "timeout"
        if "Provider" in name:
            return "provider_error"
        if "SlotUnavailable" in name:
            return "slot_unavailable"
        if "NotFound" in name:
            return "not_found"
    return "internal_error"


def _user_message_for(error_code: str) -> str:
    """A message written for a person hearing it over the phone.

    It never carries exception text, stack detail, or provider internals.
    """
    return {
        "timeout": "The booking system did not respond. I'd like to try that again.",
        "provider_error": "I'm having trouble reaching the booking system. Let me try again.",
        "slot_unavailable": "That time was just taken. Let me check what else is available.",
        "not_found": "I couldn't find a matching booking.",
        "invalid_arguments": "That request doesn't look right. Let me double-check the details.",
        "confirmation_required": "I need your confirmation before I go ahead.",
        "internal_error": "Something went wrong on my end. I'd like to try that again.",
    }.get(error_code, "Something went wrong on my end. I'd like to try that again.")


def tool(func):
    """Wrap a synchronous tool function into a :class:`ToolResult` dict.

    The decorated function receives a sqlite3.Connection as its first
    positional argument. The decorator does NOT close that connection,
    because the caller (test fixture or lifespan) owns it.

    If the caller passes ``conn=None``, the decorator opens its own
    connection and closes it in ``finally``.

    The decorator owns input validation, so a malformed call never reaches the
    tool body. The call id travels in a context variable rather than as an
    argument, which keeps every tool signature at ``(conn, input_model)``.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        call_id = kwargs.pop("_call_id", None)

        start = time.time()

        # Determine if we own the connection.
        owns_conn = not args or args[0] is None
        conn: sqlite3.Connection = db_connect() if owns_conn else args[0]

        # The call id is visible to the tool body through the context var.
        # An explicit _call_id wins; otherwise the ambient value (set by the
        # agent loop) is kept, not cleared.
        if call_id is not None:
            token = _current_call_id.set(call_id)
        else:
            token = None
        try:
            try:
                inp = _validate_input(func, args, kwargs)
            except ValidationError as exc:
                tr = ToolResult(
                    ok=False,
                    tool=func.__name__,
                    error_code="invalid_arguments",
                    message=_user_message_for("invalid_arguments"),
                    data={"detail": str(exc)},
                )
                latency_ms = int((time.time() - start) * 1000)
                _write_log(
                    conn=conn,
                    call_id=call_id,
                    tool_name=func.__name__,
                    args=args,
                    kwargs=kwargs,
                    tr=tr,
                    latency_ms=latency_ms,
                )
                return tr.to_dict()

            result = func(conn, inp, **kwargs)

            if isinstance(result, dict) and result.get("error"):
                # The tool returned an explicit error dict.
                error_code = result.get("error_code", "internal_error")
                tr = ToolResult(
                    ok=False,
                    tool=func.__name__,
                    error_code=error_code,
                    message=result.get("message") or _user_message_for(error_code),
                    injected=result.get("injected", False),
                    data=result.get("data", {}),
                )
            else:
                payload = result if isinstance(result, dict) else {"value": result}
                # The tool's own "message" is a spoken reply, not a log field.
                spoken = payload.pop("message", "") if isinstance(payload, dict) else ""
                tr = ToolResult(
                    ok=True,
                    tool=func.__name__,
                    message=spoken,
                    data=payload if isinstance(payload, dict) else {},
                )

            latency_ms = int((time.time() - start) * 1000)
            _write_log(
                conn=conn,
                call_id=call_id,
                tool_name=func.__name__,
                args=args,
                kwargs=kwargs,
                tr=tr,
                latency_ms=latency_ms,
            )
            return tr.to_dict()

        except CalendarError as exc:
            latency_ms = int((time.time() - start) * 1000)
            error_code = _error_code_from_exception(exc)
            tr = ToolResult(
                ok=False,
                tool=func.__name__,
                error_code=error_code,
                message=_user_message_for(error_code),
                injected=exc.injected,
            )
            _write_log(
                conn=conn,
                call_id=call_id,
                tool_name=func.__name__,
                args=args,
                kwargs=kwargs,
                tr=tr,
                latency_ms=latency_ms,
            )
            return tr.to_dict()

        except Exception:
            # Generic fallback — never leak raw exception text.
            latency_ms = int((time.time() - start) * 1000)
            tr = ToolResult(
                ok=False,
                tool=func.__name__,
                error_code="internal_error",
                message=_user_message_for("internal_error"),
                injected=False,
            )
            _write_log(
                conn=conn,
                call_id=call_id,
                tool_name=func.__name__,
                args=args,
                kwargs=kwargs,
                tr=tr,
                latency_ms=latency_ms,
            )
            return tr.to_dict()
        finally:
            if token is not None:
                _current_call_id.reset(token)
            if owns_conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return wrapper


def _write_log(
    *,
    conn: sqlite3.Connection,
    call_id: int | None,
    tool_name: str,
    args: tuple,
    kwargs: dict[str, Any],
    tr: ToolResult,
    latency_ms: int,
) -> None:
    """Write one tool_calls row."""
    log_tool_call(
        conn,
        call_id=call_id,
        tool_name=tool_name,
        arguments_json=_mask_args(args, kwargs),
        result_json=json.dumps(tr.to_dict(), separators=(",", ":"), default=str),
        success=tr.ok,
        latency_ms=latency_ms,
        injected=tr.injected,
    )


__all__ = ["ToolResult", "tool"]
