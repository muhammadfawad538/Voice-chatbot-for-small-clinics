"""Tool definitions for Phase 2a — Mock Calendar and Tools.

Public API:
- calendar: CalendarClient, MockCalendarClient, and all CalendarError types
- decorator: ToolResult, tool decorator
- tools: check_availability, book_slot, reschedule, cancel, handoff_to_human
  and their input models
"""

from __future__ import annotations

from app.tools.calendar import (
    CalendarClient,
    CalendarError,
    CalendarNotFoundError,
    CalendarProviderError,
    CalendarSlotUnavailableError,
    CalendarTimeoutError,
    MockCalendarClient,
)
from app.tools.decorator import ToolResult, tool
from app.tools.tools import (
    BookSlotInput,
    CancelInput,
    HandoffToHumanInput,
    RescheduleInput,
    book_slot,
    cancel,
    check_availability,
    handoff_to_human,
    reschedule,
)

__all__ = [
    # Calendar interface
    "CalendarClient",
    "CalendarError",
    "CalendarTimeoutError",
    "CalendarProviderError",
    "CalendarSlotUnavailableError",
    "CalendarNotFoundError",
    "MockCalendarClient",
    # Decorator
    "ToolResult",
    "tool",
    # Tool functions
    "check_availability",
    "book_slot",
    "reschedule",
    "cancel",
    "handoff_to_human",
    # Input models
    "CheckAvailabilityInput",
    "BookSlotInput",
    "RescheduleInput",
    "CancelInput",
    "HandoffToHumanInput",
]

# Import input models (avoids re-exporting the private helpers)
from app.tools.tools import (  # noqa: F401
    CheckAvailabilityInput,
)