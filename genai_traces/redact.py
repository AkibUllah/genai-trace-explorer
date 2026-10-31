"""Ingest-time privacy: drop message content unless asked to keep it, mask PII in what is kept.

This runs before anything is written, so a prompt containing a customer's phone number never
reaches the database when content capture is off.
"""

from __future__ import annotations

import re
from typing import Any

from .semconv import CONTENT_EVENTS, is_content_attribute

NOT_CAPTURED = "[content not captured]"

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Bangladeshi mobiles (+880 1XXXXXXXXX / 01XXXXXXXXX) and generic international numbers.
PHONE = re.compile(
    r"(?<![\w+])(?:\+?880[\s-]?|0)1[3-9]\d{2}[\s-]?\d{6}\b|\+\d{1,3}[\s-]?\d{6,12}\b"
)
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, char in enumerate(reversed(digits)):
        n = int(char)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn(digits):
        return f"[card ****{digits[-4:]}]"
    return match.group(0)


def mask_text(text: str) -> str:
    text = EMAIL.sub("[email]", text)
    text = CARD.sub(_mask_card, text)
    return PHONE.sub("[phone]", text)


def mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    return value


def scrub_attributes(attrs: dict[str, Any], capture_content: bool, redact_pii: bool) -> dict:
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if not capture_content and is_content_attribute(key):
            out[key] = NOT_CAPTURED
            continue
        out[key] = mask_value(value) if redact_pii else value
    return out


def scrub_events(events: list[dict], capture_content: bool, redact_pii: bool) -> list[dict]:
    out = []
    for event in events:
        attrs = event.get("attributes", {})
        if not capture_content and event.get("name") in CONTENT_EVENTS:
            attrs = {key: NOT_CAPTURED for key in attrs}
        else:
            attrs = scrub_attributes(attrs, capture_content, redact_pii)
        out.append({**event, "attributes": attrs})
    return out
