"""Helpers for translating Local Hub physical-device events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import (
    BUTTON_1_LONG_PRESS,
    BUTTON_1_SHORT_PRESS,
    BUTTON_2_LONG_PRESS,
    BUTTON_2_SHORT_PRESS,
    YS5708_MODELS,
)


@dataclass(frozen=True)
class ParsedButtonEvent:
    """A validated physical-button event."""

    trigger_type: str
    button: int
    press_type: str


def is_ys5708_model(model: str | None) -> bool:
    """Return True for validated YS5708 model variants."""
    normalized = str(model or "").upper()
    return any(normalized.startswith(model_id) for model_id in YS5708_MODELS)


def parse_ys5708_button_event(
    *, model: str | None, event_name: str, event_data: dict[str, Any]
) -> ParsedButtonEvent | None:
    """Translate a confirmed YS5708 Switch.DevEvent into an HA trigger type."""
    if not is_ys5708_model(model) or event_name != "Switch.DevEvent":
        return None

    nested = event_data.get("event")
    if not isinstance(nested, dict):
        return None

    button = nested.get("keyMask")
    press_type = nested.get("type")
    if button not in (1, 2) or press_type not in ("Press", "LongPress"):
        return None

    mapping = {
        (1, "Press"): BUTTON_1_SHORT_PRESS,
        (1, "LongPress"): BUTTON_1_LONG_PRESS,
        (2, "Press"): BUTTON_2_SHORT_PRESS,
        (2, "LongPress"): BUTTON_2_LONG_PRESS,
    }
    return ParsedButtonEvent(
        trigger_type=mapping[(button, press_type)],
        button=int(button),
        press_type=str(press_type),
    )
