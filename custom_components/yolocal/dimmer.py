"""Pure helpers for YoLink dimmer state conversion."""

from __future__ import annotations

from typing import Any


def yolink_brightness_to_ha(value: Any) -> int | None:
    """Convert YoLink 0..100 brightness to Home Assistant 0..255."""
    if value is None:
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None
    percent = max(0.0, min(100.0, percent))
    return round(percent * 255 / 100)


def ha_brightness_to_yolink(value: Any) -> int | None:
    """Convert Home Assistant 0..255 brightness to YoLink 0..100."""
    if value is None:
        return None
    try:
        brightness = float(value)
    except (TypeError, ValueError):
        return None
    brightness = max(0.0, min(255.0, brightness))
    return round(brightness * 100 / 255)
