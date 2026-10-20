"""Phone-number masking.

One implementation, used by the tools (to build the masked value that reaches
the database) and by the decorator (to mask whatever is written to the log).
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\d")


def mask_phone(number: str) -> str:
    """Mask a phone number for storage: (555) 123-4567 -> (XXX) XXX-4567.

    The last four digits are kept. They identify a caller well enough for a
    human to recognise a repeat caller, and not well enough to be a contact
    detail. A number that is not 10 digits is masked completely.
    """
    digits = "".join(_DIGITS.findall(number))
    if len(digits) == 10:
        return f"(XXX) XXX-{digits[-4:]}"
    if len(digits) > 10:
        # International: keep the country code visible, mask the rest.
        return f"(XXX) XXX-{digits[-4:]}"
    return "(XXX) XXX-XXXX"


def looks_like_phone(value: str) -> bool:
    """True when a string is plausibly a phone number (7+ digits)."""
    digits = "".join(_DIGITS.findall(value))
    return len(digits) >= 7
