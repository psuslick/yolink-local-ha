"""Bounded in-memory capture of diagnostic YoLink Local MQTT device events."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

DEFAULT_EVENT_CAPTURE_LIMIT = 100
TARGET_MODEL_PREFIXES = ("YS5708", "YS5707")
SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
)
BUTTON_KEY_NAMES = {
    "keymask",
    "key",
    "button",
    "buttonid",
    "button_id",
    "presstype",
    "press_type",
    "action",
}


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe representation while redacting obvious secrets."""
    if depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
                result[text_key] = "**REDACTED**"
            else:
                result[text_key] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth=depth + 1) for item in value]
    return repr(value)


def extract_button_candidates(value: Any, path: str = "") -> list[dict[str, Any]]:
    """Extract fields that may describe a physical button action."""
    candidates: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            text_key = str(key)
            child_path = f"{path}.{text_key}" if path else text_key
            if text_key.lower() in BUTTON_KEY_NAMES:
                candidates.append(
                    {
                        "path": child_path,
                        "key": text_key,
                        "value": _safe_value(item),
                    }
                )
            candidates.extend(extract_button_candidates(item, child_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            candidates.extend(extract_button_candidates(item, child_path))
    return candidates


class DiagnosticEventCapture:
    """Keep a small RAM-only event history for protocol/device diagnostics."""

    def __init__(self, limit: int = DEFAULT_EVENT_CAPTURE_LIMIT) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=limit)
        self._limit = limit
        self._total_captured = 0

    def should_capture(self, device: Any, event: Any) -> bool:
        """Return True for target models or payloads that look button-related."""
        model = str(getattr(device, "model", "") or "").upper()
        if any(model.startswith(prefix) for prefix in TARGET_MODEL_PREFIXES):
            return True

        raw = getattr(event, "raw", None)
        data = getattr(event, "data", None)
        return bool(extract_button_candidates(raw) or extract_button_candidates(data))

    def record(self, device: Any, event: Any) -> dict[str, Any] | None:
        """Record one relevant MQTT device event and return the stored record."""
        if not self.should_capture(device, event):
            return None

        raw = _safe_value(getattr(event, "raw", None))
        data = _safe_value(getattr(event, "data", None))
        record = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "device_id": str(getattr(device, "device_id", "") or ""),
            "device_name": str(getattr(device, "name", "") or ""),
            "device_type": str(getattr(device, "device_type", "") or ""),
            "model": str(getattr(device, "model", "") or ""),
            "event_name": str(getattr(event, "event", "") or ""),
            "event_data": data,
            "raw": raw,
            "button_candidates": extract_button_candidates(raw),
        }
        self._events.append(record)
        self._total_captured += 1
        return record

    def diagnostics(self) -> dict[str, Any]:
        """Return a serializable snapshot for Download Diagnostics."""
        return {
            "capture_version": 1,
            "storage": "ram_only",
            "limit": self._limit,
            "stored_event_count": len(self._events),
            "total_captured_since_start": self._total_captured,
            "target_model_prefixes": list(TARGET_MODEL_PREFIXES),
            "events": list(self._events),
        }
