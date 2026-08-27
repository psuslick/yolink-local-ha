"""Regression tests for bounded RAM-only MQTT event capture."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "yolocal"
    / "event_capture.py"
)
_SPEC = importlib.util.spec_from_file_location("yolocal_event_capture_test", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)

DiagnosticEventCapture = _MOD.DiagnosticEventCapture
extract_button_candidates = _MOD.extract_button_candidates


def test_ys5708_event_is_captured_even_before_payload_format_is_known() -> None:
    capture = DiagnosticEventCapture(limit=10)
    device = SimpleNamespace(
        device_id="abc",
        name="Bathroom Fan",
        device_type="Switch",
        model="YS5708-UC (Switch)",
    )
    event = SimpleNamespace(
        event="Switch.StatusChange",
        data={"event": {"keyMask": 2, "type": "Press"}},
        raw={"event": "Switch.StatusChange", "data": {"event": {"keyMask": 2, "type": "Press"}}},
    )

    stored = capture.record(device, event)
    assert stored is not None
    assert stored["model"].startswith("YS5708")
    assert any(item["key"] == "keyMask" for item in stored["button_candidates"])


def test_non_target_event_without_button_fields_is_ignored() -> None:
    capture = DiagnosticEventCapture(limit=10)
    device = SimpleNamespace(
        device_id="temp",
        name="Temp",
        device_type="THSensor",
        model="YS8005-UC (THSensor)",
    )
    event = SimpleNamespace(
        event="THSensor.Report",
        data={"temperature": 72.1},
        raw={"data": {"temperature": 72.1}},
    )
    assert capture.record(device, event) is None


def test_button_like_event_from_unknown_model_is_captured() -> None:
    capture = DiagnosticEventCapture(limit=10)
    device = SimpleNamespace(
        device_id="future",
        name="Future Switch",
        device_type="Switch",
        model="Unknown",
    )
    event = SimpleNamespace(
        event="Switch.Event",
        data={"button": 1, "pressType": "long"},
        raw={"data": {"button": 1, "pressType": "long"}},
    )
    assert capture.record(device, event) is not None


def test_capture_is_bounded_and_ram_only_metadata_is_reported() -> None:
    capture = DiagnosticEventCapture(limit=2)
    device = SimpleNamespace(
        device_id="abc",
        name="Bathroom Fan",
        device_type="Switch",
        model="YS5708-UC (Switch)",
    )
    for index in range(3):
        event = SimpleNamespace(
            event="Switch.StatusChange",
            data={"sequence": index},
            raw={"sequence": index},
        )
        capture.record(device, event)

    diag = capture.diagnostics()
    assert diag["storage"] == "ram_only"
    assert diag["stored_event_count"] == 2
    assert diag["total_captured_since_start"] == 3
    assert [e["event_data"]["sequence"] for e in diag["events"]] == [1, 2]


def test_obvious_secret_keys_are_redacted() -> None:
    candidates = extract_button_candidates({"event": {"keyMask": 1, "type": "Press"}})
    assert any(item["key"] == "keyMask" for item in candidates)

    capture = DiagnosticEventCapture(limit=2)
    device = SimpleNamespace(device_id="abc", name="Fan", device_type="Switch", model="YS5708")
    event = SimpleNamespace(event="Switch.Event", data={}, raw={"accessToken": "secret", "keyMask": 1})
    stored = capture.record(device, event)
    assert stored is not None
    assert stored["raw"]["accessToken"] == "**REDACTED**"
