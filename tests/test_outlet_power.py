"""Regression tests for YS6614 active-power telemetry handling."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "yolocal"
    / "outlet_power.py"
)
spec = importlib.util.spec_from_file_location("yolocal_outlet_power_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

BASE = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def test_power_field_is_deciwatts_and_watt_is_not_a_fallback() -> None:
    payload = {"state": {"state": "open", "power": 445, "watt": 0}}
    present, power = mod.power_field(payload)
    assert present is True
    assert power == 445.0
    assert power / 10.0 == 44.5

    no_power = {"state": {"state": "open", "watt": 999}}
    present, power = mod.power_field(no_power)
    assert present is False
    assert power is None


def test_null_or_invalid_power_is_removed_instead_of_overwriting_cache() -> None:
    for bad in (None, "", "not-a-number", float("nan"), -1, True):
        incoming = mod.sanitize_outlet_power_payload(
            {"state": {"state": "open", "power": bad, "watt": 0}}
        )
        assert "power" not in incoming["state"]

        existing = {"state": {"state": "open", "power": 445.0, "watt": 0}}
        merged = {
            **existing,
            **incoming,
            "state": {**existing["state"], **incoming["state"]},
        }
        assert merged["state"]["power"] == 445.0


def test_sparse_status_change_preserves_previous_power() -> None:
    existing = {"state": {"state": "open", "power": 445.0, "watt": 0}}
    incoming = mod.sanitize_outlet_power_payload(
        {"state": "open", "alertType": {"overload": False}}
    )
    canonical_incoming = {
        "state": {
            "state": incoming["state"],
            "alertType": incoming["alertType"],
        }
    }
    merged = {
        **existing,
        **canonical_incoming,
        "state": {**existing["state"], **canonical_incoming["state"]},
    }
    assert merged["state"]["power"] == 445.0


def test_explicit_relay_off_forces_active_power_to_zero() -> None:
    nested = mod.sanitize_outlet_power_payload(
        {"state": {"state": "closed", "watt": 1234}}
    )
    assert nested["state"]["power"] == 0.0

    flat = mod.sanitize_outlet_power_payload({"state": "closed"})
    assert flat["power"] == 0.0


def test_tracker_refreshes_only_on_outlets_with_stale_power() -> None:
    tracker = mod.OutletPowerTracker()
    tracker.ensure_device("outlet", name="Outlet", model="YS6614-UC")

    assert tracker.should_refresh("outlet", outlet_is_on=False, now=BASE) is False
    assert tracker.should_refresh("outlet", outlet_is_on=True, now=BASE) is True

    tracker.observe_payload(
        "outlet", {"state": {"state": "open", "power": 3000}}, now=BASE
    )
    assert (
        tracker.should_refresh(
            "outlet", outlet_is_on=True, now=BASE + timedelta(minutes=4, seconds=59)
        )
        is False
    )
    assert (
        tracker.should_refresh(
            "outlet", outlet_is_on=True, now=BASE + timedelta(minutes=5)
        )
        is True
    )


def test_just_powered_on_outlet_does_not_wait_on_prior_off_zero() -> None:
    tracker = mod.OutletPowerTracker()
    tracker.observe_payload("outlet", {"state": "closed"}, now=BASE)
    assert tracker.should_refresh("outlet", outlet_is_on=True, now=BASE) is False

    tracker.mark_on_without_measurement("outlet", now=BASE + timedelta(seconds=1))
    assert (
        tracker.should_refresh(
            "outlet", outlet_is_on=True, now=BASE + timedelta(seconds=1)
        )
        is True
    )


def test_failed_refresh_is_rate_limited_without_erasing_power() -> None:
    tracker = mod.OutletPowerTracker()
    tracker.observe_payload(
        "outlet", {"state": {"state": "open", "power": 2500}}, now=BASE
    )
    due = BASE + timedelta(minutes=5)
    assert tracker.should_refresh("outlet", outlet_is_on=True, now=due)

    tracker.note_refresh_requested("outlet", now=due)
    tracker.note_refresh_failure(
        "outlet", error="000201", unreachable=True, now=due + timedelta(seconds=1)
    )

    assert (
        tracker.should_refresh(
            "outlet", outlet_is_on=True, now=due + timedelta(minutes=4)
        )
        is False
    )
    diag = tracker.diagnostics()["outlet"]
    assert diag["last_valid_power_deciwatts"] == 2500.0
    assert diag["refresh_unreachable"] == 1


def test_invalid_sample_does_not_become_fresh_power() -> None:
    tracker = mod.OutletPowerTracker()
    assert (
        tracker.observe_payload(
            "outlet", {"state": {"state": "open", "power": None}}, now=BASE
        )
        is False
    )
    diag = tracker.diagnostics()["outlet"]
    assert diag["last_valid_power_at"] is None
    assert diag["invalid_power_samples"] == 1
