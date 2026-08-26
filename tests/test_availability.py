"""Regression tests for the MQTT-first availability state machine."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "yolocal"
    / "availability.py"
)
_SPEC = importlib.util.spec_from_file_location("yolocal_availability_test", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)

AvailabilityManager = _MOD.AvailabilityManager
DEFAULT_STALE_AFTER = _MOD.DEFAULT_STALE_AFTER
STATUS_SUSPECT = _MOD.STATUS_SUSPECT
STATUS_UNAVAILABLE = _MOD.STATUS_UNAVAILABLE

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_transient_000201_does_not_make_fresh_device_unavailable() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    manager.record_mqtt_event("outlet", now=BASE)

    for offset in (1, 3, 5):
        manager.record_device_unreachable(
            "outlet", now=BASE + timedelta(minutes=offset)
        )

    assert manager.is_available("outlet")
    assert manager.status("outlet") == STATUS_SUSPECT


def test_stale_plus_three_spaced_failures_becomes_unavailable() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    for i in range(7):
        manager.record_mqtt_event("outlet", now=BASE + timedelta(minutes=5 * i))

    stale_at = BASE + timedelta(minutes=61)
    evaluation = manager.evaluate("outlet", now=stale_at)
    assert evaluation.should_verify
    assert manager.is_available("outlet")

    for offset in (0, 2, 4):
        manager.record_device_unreachable(
            "outlet", now=stale_at + timedelta(minutes=offset)
        )

    assert not manager.is_available("outlet")
    assert manager.status("outlet") == STATUS_UNAVAILABLE


def test_http_success_immediately_recovers_unavailable_device() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    manager.record_mqtt_event("outlet", now=BASE)
    stale_at = BASE + timedelta(hours=13)
    for offset in (0, 2, 4):
        manager.record_device_unreachable(
            "outlet", now=stale_at + timedelta(minutes=offset)
        )
    assert not manager.is_available("outlet")

    manager.record_http_success("outlet", now=stale_at + timedelta(minutes=5))
    assert manager.is_available("outlet")


def test_mqtt_immediately_recovers_unavailable_device() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    manager.record_mqtt_event("outlet", now=BASE)
    stale_at = BASE + timedelta(hours=13)
    for offset in (0, 2, 4):
        manager.record_device_unreachable(
            "outlet", now=stale_at + timedelta(minutes=offset)
        )
    assert not manager.is_available("outlet")

    manager.record_mqtt_event("outlet", now=stale_at + timedelta(minutes=5))
    assert manager.is_available("outlet")


def test_line_powered_stable_five_minute_cadence_learns_30_minutes() -> None:
    manager = AvailabilityManager()
    record = manager.ensure_device("outlet", device_type="Outlet")
    for i in range(7):
        manager.record_mqtt_event("outlet", now=BASE + timedelta(minutes=5 * i))

    threshold, source = record.stale_after()
    assert threshold == timedelta(minutes=30)
    assert source == "adaptive_mqtt"


def test_battery_device_keeps_conservative_12_hour_threshold() -> None:
    manager = AvailabilityManager()
    record = manager.ensure_device("temp", device_type="THSensor")
    for i in range(7):
        manager.record_mqtt_event("temp", now=BASE + timedelta(minutes=5 * i))

    threshold, source = record.stale_after()
    assert threshold == DEFAULT_STALE_AFTER
    assert source == "default_12h"


def test_irregular_line_powered_cadence_falls_back_to_12_hours() -> None:
    manager = AvailabilityManager()
    record = manager.ensure_device("outlet", device_type="Outlet")
    offsets = [0, 1, 10, 11, 30, 31, 60]
    for minutes in offsets:
        manager.record_mqtt_event("outlet", now=BASE + timedelta(minutes=minutes))

    threshold, source = record.stale_after()
    assert threshold == DEFAULT_STALE_AFTER
    assert source == "default_12h_irregular_cadence"


def test_command_failure_does_not_make_device_unavailable() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    manager.record_mqtt_event("outlet", now=BASE)

    manager.record_command_failure(
        "outlet",
        error_code="000201",
        error="Cannot connect to the device",
        now=BASE + timedelta(minutes=1),
    )

    assert manager.is_available("outlet")
    assert manager.status("outlet") == STATUS_SUSPECT


def test_transport_error_does_not_count_as_device_failure() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    manager.record_mqtt_event("outlet", now=BASE)

    manager.record_transport_error(
        "outlet", error="connection reset", source="verification_transport",
        now=BASE + timedelta(minutes=1)
    )
    diag = manager.diagnostics()["outlet"]
    assert diag["consecutive_device_failures"] == 0
    assert manager.is_available("outlet")


def test_offline_hint_is_suspect_not_immediate_unavailable() -> None:
    manager = AvailabilityManager()
    manager.ensure_device("outlet", device_type="Outlet")
    manager.record_mqtt_event("outlet", now=BASE)

    manager.record_offline_hint("outlet", source="mqtt", now=BASE + timedelta(minutes=1))
    assert manager.is_available("outlet")
    assert manager.status("outlet") == STATUS_SUSPECT
