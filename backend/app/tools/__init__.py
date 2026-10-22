"""Tool definitions for Phase 2a/2b — Calendar, Handoff, Knowledge.

Public API:
- calendar: CalendarClient, MockCalendarClient, and all CalendarError types
- decorator: ToolResult, tool decorator
- tools: check_availability, book_slot, reschedule, cancel, handoff_to_human,
  search_knowledge and their input models
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
from app.tools.knowledge import (
    SearchKnowledgeInput,
    SearchKnowledgeOutput,
    search_knowledge,
)
from app.tools.tools import (
    BookSlotInput,
    CancelInput,
    CheckAvailabilityInput,
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
    "search_knowledge",
    # Input models
    "CheckAvailabilityInput",
    "BookSlotInput",
    "RescheduleInput",
    "CancelInput",
    "HandoffToHumanInput",
    "SearchKnowledgeInput",
    "SearchKnowledgeOutput",
]