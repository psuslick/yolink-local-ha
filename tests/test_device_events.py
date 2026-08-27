"""Regression tests for validated YS5708 physical-button events."""

import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "yolocal"

# Load const + device_events without importing Home Assistant.
pkg = types.ModuleType("yolocal")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("yolocal", pkg)

for name in ("const", "device_events"):
    spec = importlib.util.spec_from_file_location(f"yolocal.{name}", ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

mod = sys.modules["yolocal.device_events"]
parse = mod.parse_ys5708_button_event


def _parse(button: int, press: str):
    return parse(
        model="YS5708-UC",
        event_name="Switch.DevEvent",
        event_data={"event": {"keyMask": button, "type": press}},
    )


def test_button_1_short_press() -> None:
    event = _parse(1, "Press")
    assert event is not None
    assert event.trigger_type == "button_1_short_press"


def test_button_1_long_press() -> None:
    event = _parse(1, "LongPress")
    assert event is not None
    assert event.trigger_type == "button_1_long_press"


def test_button_2_short_press() -> None:
    event = _parse(2, "Press")
    assert event is not None
    assert event.trigger_type == "button_2_short_press"


def test_button_2_long_press() -> None:
    event = _parse(2, "LongPress")
    assert event is not None
    assert event.trigger_type == "button_2_long_press"


def test_non_dev_event_is_not_interpreted_as_button() -> None:
    assert parse(
        model="YS5708-UC",
        event_name="Switch.StatusChange",
        event_data={"event": {"keyMask": 1, "type": "Press"}},
    ) is None


def test_unvalidated_model_does_not_get_ys5708_trigger() -> None:
    assert parse(
        model="YS8005-UC",
        event_name="Switch.DevEvent",
        event_data={"event": {"keyMask": 1, "type": "Press"}},
    ) is None
