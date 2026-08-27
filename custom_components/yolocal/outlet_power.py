"""Targeted active-power telemetry tracking for YoLink outlets.

YoLink Outlet ``power`` is the instantaneous active-power field in deciwatts.
The similarly named ``watt`` field is not a substitute for instantaneous power.

The Local Hub can emit sparse ``Outlet.StatusChange`` MQTT reports that contain
relay state but no active-power sample.  This module keeps telemetry freshness
separate from device availability so those sparse reports do not erase a valid
power measurement and so stale/missing power can be refreshed with one targeted
serialized ``getState`` request rather than restoring the old all-device poll.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

OUTLET_POWER_STALE_AFTER = timedelta(minutes=5)
OUTLET_POWER_REFRESH_RETRY_AFTER = timedelta(minutes=5)


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def coerce_power_deciwatts(value: Any) -> float | None:
    """Return a validated Outlet active-power value in deciwatts.

    ``None``, booleans, malformed strings, NaN/Inf, and negative values are not
    valid measurements.  A numeric zero is valid and must be preserved.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number) or number < 0:
        return None
    return number


def relay_is_on(payload: dict[str, Any]) -> bool | None:
    """Return the explicit relay state from flat or canonical state payloads."""
    state = payload.get("state")
    if isinstance(state, dict):
        state = state.get("state")
    if state == "open":
        return True
    if state in {"closed", "close"}:
        return False
    return None


def power_field(payload: dict[str, Any]) -> tuple[bool, float | None]:
    """Return ``(present, valid_value)`` for active ``power`` in a payload."""
    state = payload.get("state")
    if isinstance(state, dict) and "power" in state:
        return True, coerce_power_deciwatts(state.get("power"))
    if "power" in payload:
        return True, coerce_power_deciwatts(payload.get("power"))
    return False, None


def sanitize_outlet_power_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy that cannot erase valid power with null/malformed telemetry.

    Sparse payloads keep ``power`` absent so the coordinator's merge preserves
    the previous value.  An explicit relay-off state is special: active power is
    physically known to be zero and is therefore injected as ``power: 0``.
    """
    sanitized = dict(payload)
    state = sanitized.get("state")

    if isinstance(state, dict):
        nested = dict(state)
        if "power" in nested:
            power = coerce_power_deciwatts(nested.get("power"))
            if power is None:
                nested.pop("power", None)
            else:
                nested["power"] = power
        if nested.get("state") in {"closed", "close"}:
            nested["power"] = 0.0
        sanitized["state"] = nested
        return sanitized

    if "power" in sanitized:
        power = coerce_power_deciwatts(sanitized.get("power"))
        if power is None:
            sanitized.pop("power", None)
        else:
            sanitized["power"] = power
    if sanitized.get("state") in {"closed", "close"}:
        sanitized["power"] = 0.0
    return sanitized


@dataclass(slots=True)
class OutletPowerRecord:
    """In-memory active-power telemetry health for one outlet."""

    device_id: str
    name: str | None = None
    model: str | None = None
    last_valid_power_at: datetime | None = None
    last_valid_power_deciwatts: float | None = None
    last_refresh_requested_at: datetime | None = None
    last_refresh_success_at: datetime | None = None
    last_refresh_failure_at: datetime | None = None
    refresh_requests: int = 0
    refresh_successes: int = 0
    refresh_failures: int = 0
    refresh_unreachable: int = 0
    invalid_power_samples: int = 0
    last_refresh_error: str | None = None


class OutletPowerTracker:
    """Track power freshness independently from device availability."""

    def __init__(self) -> None:
        self._records: dict[str, OutletPowerRecord] = {}

    def ensure_device(
        self,
        device_id: str,
        *,
        name: str | None = None,
        model: str | None = None,
    ) -> OutletPowerRecord:
        record = self._records.get(device_id)
        if record is None:
            record = OutletPowerRecord(device_id=device_id)
            self._records[device_id] = record
        if name is not None:
            record.name = name
        if model is not None:
            record.model = model
        return record

    def remove_device(self, device_id: str) -> None:
        self._records.pop(device_id, None)

    def observe_payload(
        self,
        device_id: str,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        """Record a genuine incoming power sample or explicit off=0 observation.

        Returns True only when the incoming payload itself established a valid
        active-power value.  A sparse state report is intentionally not counted
        as fresh power merely because a cached value survives the merge.
        """
        now = _coerce_utc(now)
        record = self.ensure_device(device_id)
        present, power = power_field(payload)
        relay_on = relay_is_on(payload)

        if present and power is None:
            record.invalid_power_samples += 1

        if power is None and relay_on is False:
            power = 0.0

        if power is None:
            return False

        record.last_valid_power_at = now
        record.last_valid_power_deciwatts = power
        record.last_refresh_error = None
        return True

    def mark_on_without_measurement(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Make a just-powered-on outlet eligible for a targeted refresh.

        A prior zero from the relay-off state must not postpone the first real
        load measurement for another full stale interval.
        """
        record = self.ensure_device(device_id)
        record.last_valid_power_at = None
        # Keep the previous value for diagnostics only.  Coordinator state is
        # independently set/preserved and will be refreshed by getState.
        if record.last_refresh_requested_at is not None:
            now_utc = _coerce_utc(now)
            if now_utc - record.last_refresh_requested_at >= OUTLET_POWER_REFRESH_RETRY_AFTER:
                record.last_refresh_requested_at = None

    def should_refresh(
        self,
        device_id: str,
        *,
        outlet_is_on: bool | None,
        now: datetime | None = None,
    ) -> bool:
        """Return whether one targeted power refresh is due."""
        if outlet_is_on is not True:
            return False
        now = _coerce_utc(now)
        record = self.ensure_device(device_id)
        if (
            record.last_refresh_requested_at is not None
            and now - record.last_refresh_requested_at < OUTLET_POWER_REFRESH_RETRY_AFTER
        ):
            return False
        return (
            record.last_valid_power_at is None
            or now - record.last_valid_power_at >= OUTLET_POWER_STALE_AFTER
        )

    def note_refresh_requested(
        self, device_id: str, *, now: datetime | None = None
    ) -> None:
        record = self.ensure_device(device_id)
        record.refresh_requests += 1
        record.last_refresh_requested_at = _coerce_utc(now)

    def note_refresh_success(
        self,
        device_id: str,
        *,
        had_power_measurement: bool,
        now: datetime | None = None,
    ) -> None:
        record = self.ensure_device(device_id)
        now_utc = _coerce_utc(now)
        if had_power_measurement:
            record.refresh_successes += 1
            record.last_refresh_success_at = now_utc
            record.last_refresh_error = None
        else:
            record.refresh_failures += 1
            record.last_refresh_failure_at = now_utc
            record.last_refresh_error = "successful_getState_without_power"

    def note_refresh_failure(
        self,
        device_id: str,
        *,
        error: str,
        unreachable: bool = False,
        now: datetime | None = None,
    ) -> None:
        record = self.ensure_device(device_id)
        record.refresh_failures += 1
        if unreachable:
            record.refresh_unreachable += 1
        record.last_refresh_failure_at = _coerce_utc(now)
        record.last_refresh_error = error

    def diagnostics(self) -> dict[str, Any]:
        """Return compact read-only telemetry diagnostics."""
        now = utcnow()
        output: dict[str, Any] = {}
        for device_id, record in sorted(self._records.items()):
            age = (
                (now - record.last_valid_power_at).total_seconds()
                if record.last_valid_power_at is not None
                else None
            )
            output[device_id] = {
                "name": record.name,
                "model": record.model,
                "last_valid_power_at": _iso(record.last_valid_power_at),
                "last_valid_power_age_seconds": round(age, 1)
                if age is not None
                else None,
                "last_valid_power_deciwatts": record.last_valid_power_deciwatts,
                "last_valid_power_watts": (
                    round(record.last_valid_power_deciwatts / 10.0, 3)
                    if record.last_valid_power_deciwatts is not None
                    else None
                ),
                "stale_after_seconds": OUTLET_POWER_STALE_AFTER.total_seconds(),
                "last_refresh_requested_at": _iso(record.last_refresh_requested_at),
                "last_refresh_success_at": _iso(record.last_refresh_success_at),
                "last_refresh_failure_at": _iso(record.last_refresh_failure_at),
                "refresh_requests": record.refresh_requests,
                "refresh_successes": record.refresh_successes,
                "refresh_failures": record.refresh_failures,
                "refresh_unreachable": record.refresh_unreachable,
                "invalid_power_samples": record.invalid_power_samples,
                "last_refresh_error": record.last_refresh_error,
            }
        return output
