"""Availability state machine for YoLink Local devices.

This module intentionally separates *device availability* from the result of any
single synchronous hub-to-device request.  A transient ``000201`` response means
that one request could not reach the LoRa device; it is not, by itself, proof
that the device is offline.

The manager is deliberately in-memory only.  Home Assistant already persists
entity state through Recorder when enabled, and the companion YoLink Watchdog
can persist diagnostic outage history.  Keeping this state in memory avoids
additional writes while still making the integration restart-safe: the initial
HTTP bootstrap and subsequent MQTT reports rebuild liveness state.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

DEFAULT_STALE_AFTER = timedelta(hours=12)
MIN_ADAPTIVE_STALE_AFTER = timedelta(minutes=30)
MAX_ADAPTIVE_STALE_AFTER = DEFAULT_STALE_AFTER
MIN_ADAPTIVE_INTERVAL_SAMPLES = 5
MAX_INTERVAL_SAMPLES = 12
MAX_STABLE_MAD_RATIO = 0.35
ADAPTIVE_INTERVAL_MULTIPLIER = 6.0

DEVICE_FAILURE_THRESHOLD = 3
VERIFY_RETRY_AFTER = timedelta(minutes=2)
COMMAND_FAILURE_VERIFY_AFTER = timedelta(seconds=30)
UNAVAILABLE_RECHECK_AFTER = timedelta(minutes=15)

# These are normally line-powered and therefore good candidates for learning a
# periodic MQTT cadence.  Battery/event-driven devices retain the conservative
# 12-hour fallback unless a future implementation has model-specific evidence.
ADAPTIVE_DEVICE_TYPES = frozenset({"Outlet", "Switch", "Dimmer", "MultiOutlet"})

STATUS_UNKNOWN = "unknown"
STATUS_AVAILABLE = "available"
STATUS_SUSPECT = "suspect"
STATUS_UNAVAILABLE = "unavailable"


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | str | None) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(slots=True)
class Evaluation:
    """Result of evaluating one device's availability."""

    status_changed: bool = False
    should_verify: bool = False
    reason: str | None = None


@dataclass(slots=True)
class DeviceAvailability:
    """Mutable availability record for one YoLink device."""

    device_id: str
    name: str | None = None
    device_type: str | None = None
    model: str | None = None

    status: str = STATUS_UNKNOWN
    reason: str = "no_valid_state"

    last_liveness_at: datetime | None = None
    last_mqtt_at: datetime | None = None
    last_http_success_at: datetime | None = None
    last_command_success_at: datetime | None = None
    last_reported_at: datetime | None = None
    last_mqtt_arrival_at: datetime | None = None

    raw_online_hint: bool | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    last_error_source: str | None = None

    consecutive_device_failures: int = 0
    next_verification_at: datetime | None = None

    verification_requests: int = 0
    verification_successes: int = 0
    verification_unreachable: int = 0
    command_failures: int = 0
    command_successes: int = 0

    mqtt_intervals_seconds: deque[float] = field(
        default_factory=lambda: deque(maxlen=MAX_INTERVAL_SAMPLES)
    )

    def stale_after(self) -> tuple[timedelta, str]:
        """Return the current stale threshold and how it was selected."""
        if self.device_type not in ADAPTIVE_DEVICE_TYPES:
            return DEFAULT_STALE_AFTER, "default_12h"

        samples = list(self.mqtt_intervals_seconds)
        if len(samples) < MIN_ADAPTIVE_INTERVAL_SAMPLES:
            return DEFAULT_STALE_AFTER, "default_12h_learning"

        med = median(samples)
        if med <= 0:
            return DEFAULT_STALE_AFTER, "default_12h_invalid_cadence"

        deviations = [abs(sample - med) for sample in samples]
        mad = median(deviations)
        mad_ratio = mad / med if med else 1.0
        if mad_ratio > MAX_STABLE_MAD_RATIO:
            return DEFAULT_STALE_AFTER, "default_12h_irregular_cadence"

        seconds = med * ADAPTIVE_INTERVAL_MULTIPLIER
        seconds = max(seconds, MIN_ADAPTIVE_STALE_AFTER.total_seconds())
        seconds = min(seconds, MAX_ADAPTIVE_STALE_AFTER.total_seconds())
        return timedelta(seconds=seconds), "adaptive_mqtt"

    def is_available(self) -> bool:
        """Return the HA-facing availability decision."""
        return self.last_liveness_at is not None and self.status != STATUS_UNAVAILABLE


class AvailabilityManager:
    """Track device liveness independently from transient request failures."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceAvailability] = {}

    def ensure_device(
        self,
        device_id: str,
        *,
        name: str | None = None,
        device_type: str | None = None,
        model: str | None = None,
    ) -> DeviceAvailability:
        """Create or update a device record."""
        record = self._devices.get(device_id)
        if record is None:
            record = DeviceAvailability(device_id=device_id)
            self._devices[device_id] = record
        if name is not None:
            record.name = name
        if device_type is not None:
            record.device_type = device_type
        if model is not None:
            record.model = model
        return record

    def remove_device(self, device_id: str) -> None:
        """Forget a device removed from the Local Hub."""
        self._devices.pop(device_id, None)

    def is_available(self, device_id: str) -> bool:
        """Return the HA-facing availability decision for a device."""
        record = self._devices.get(device_id)
        return record.is_available() if record is not None else False

    def status(self, device_id: str) -> str:
        """Return the internal availability status."""
        record = self._devices.get(device_id)
        return record.status if record is not None else STATUS_UNKNOWN

    def report_timestamp_is_newer(
        self, device_id: str, reported_at: datetime | str | None
    ) -> bool:
        """Return True when a payload carries a newer device report timestamp."""
        incoming = _coerce_utc(reported_at)
        if incoming is None:
            return False
        record = self.ensure_device(device_id)
        return record.last_reported_at is None or incoming > record.last_reported_at

    def record_http_success(
        self,
        device_id: str,
        *,
        reported_at: datetime | str | None = None,
        raw_online: bool | None = None,
        source: str = "http",
        now: datetime | None = None,
    ) -> bool:
        """Record a successful synchronous getState response.

        A successful getState is positive evidence that the Local Hub could
        communicate with the device.  Therefore it restores availability even
        when a raw ``online`` field is false or absent.  The raw hint is kept in
        diagnostics, but it does not override observed liveness.
        """
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status

        record.last_liveness_at = now
        record.last_http_success_at = now
        parsed_reported = _coerce_utc(reported_at)
        if parsed_reported is not None:
            record.last_reported_at = parsed_reported
        record.raw_online_hint = raw_online
        record.consecutive_device_failures = 0
        record.next_verification_at = None
        record.last_error_code = None
        record.last_error = None
        record.last_error_source = None
        record.status = STATUS_AVAILABLE
        record.reason = f"{source}_success"
        if source == "verification":
            record.verification_successes += 1
        return previous != record.status

    def record_mqtt_event(
        self,
        device_id: str,
        *,
        reported_at: datetime | str | None = None,
        raw_online: bool | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record a non-duplicate MQTT device event as liveness evidence."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status

        if record.last_mqtt_arrival_at is not None:
            interval = (now - record.last_mqtt_arrival_at).total_seconds()
            # Ignore pathological intervals caused by clock jumps or duplicate
            # event handling.  Duplicate payloads are already filtered by the
            # coordinator before reaching this method.
            if 1.0 <= interval <= DEFAULT_STALE_AFTER.total_seconds():
                record.mqtt_intervals_seconds.append(interval)
        record.last_mqtt_arrival_at = now
        record.last_mqtt_at = now
        record.last_liveness_at = now

        parsed_reported = _coerce_utc(reported_at)
        if parsed_reported is not None:
            record.last_reported_at = parsed_reported
        record.raw_online_hint = raw_online
        record.consecutive_device_failures = 0
        record.next_verification_at = None
        record.last_error_code = None
        record.last_error = None
        record.last_error_source = None
        record.status = STATUS_AVAILABLE
        record.reason = "mqtt_report"
        return previous != record.status

    def record_offline_hint(
        self,
        device_id: str,
        *,
        source: str,
        now: datetime | None = None,
    ) -> bool:
        """Record a hub/MQTT offline hint without immediately failing HA entities."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status
        record.raw_online_hint = False
        record.last_error_source = source
        if record.last_liveness_at is None:
            record.status = STATUS_UNKNOWN
            record.reason = f"{source}_offline_hint_no_liveness"
        else:
            record.status = STATUS_SUSPECT
            record.reason = f"{source}_offline_hint"
        record.next_verification_at = now
        return previous != record.status

    def record_device_unreachable(
        self,
        device_id: str,
        *,
        error_code: str = "000201",
        error: str | None = None,
        source: str = "verification",
        now: datetime | None = None,
    ) -> bool:
        """Record one device-specific synchronous communication failure."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status

        record.last_error_code = error_code
        record.last_error = error
        record.last_error_source = source
        record.consecutive_device_failures += 1
        if source == "verification":
            record.verification_unreachable += 1

        threshold, _ = record.stale_after()
        liveness_is_stale = (
            record.last_liveness_at is None
            or now - record.last_liveness_at > threshold
        )

        if (
            record.consecutive_device_failures >= DEVICE_FAILURE_THRESHOLD
            and liveness_is_stale
        ):
            record.status = STATUS_UNAVAILABLE
            record.reason = "stale_and_repeated_000201"
            record.next_verification_at = now + UNAVAILABLE_RECHECK_AFTER
        elif record.last_liveness_at is None:
            record.status = STATUS_UNKNOWN
            record.reason = "000201_before_first_valid_state"
            record.next_verification_at = now + VERIFY_RETRY_AFTER
        else:
            record.status = STATUS_SUSPECT
            record.reason = "transient_000201"
            record.next_verification_at = now + VERIFY_RETRY_AFTER

        return previous != record.status

    def record_transport_error(
        self,
        device_id: str,
        *,
        error: str,
        source: str,
        now: datetime | None = None,
    ) -> bool:
        """Record a hub/API transport error without counting it against the device."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status
        record.last_error = error
        record.last_error_source = source
        if record.last_liveness_at is not None and record.status == STATUS_AVAILABLE:
            record.status = STATUS_SUSPECT
            record.reason = f"{source}_transport_error"
        record.next_verification_at = now + VERIFY_RETRY_AFTER
        return previous != record.status

    def record_command_success(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Record a successful device command as liveness evidence."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status
        record.command_successes += 1
        record.last_liveness_at = now
        record.last_command_success_at = now
        record.consecutive_device_failures = 0
        record.next_verification_at = None
        record.last_error_code = None
        record.last_error = None
        record.last_error_source = None
        record.status = STATUS_AVAILABLE
        record.reason = "command_success"
        return previous != record.status

    def record_command_failure(
        self,
        device_id: str,
        *,
        error_code: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record a failed command without declaring the whole device offline."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status
        record.command_failures += 1
        record.last_error_code = error_code
        record.last_error = error
        record.last_error_source = "command"
        if record.last_liveness_at is not None:
            record.status = STATUS_SUSPECT
            record.reason = "command_failed_needs_verification"
        else:
            record.status = STATUS_UNKNOWN
            record.reason = "command_failed_no_liveness"
        record.next_verification_at = now + COMMAND_FAILURE_VERIFY_AFTER
        return previous != record.status

    def note_verification_request(self, device_id: str) -> None:
        """Increment the in-memory verification counter."""
        self.ensure_device(device_id).verification_requests += 1

    def evaluate(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> Evaluation:
        """Evaluate staleness and decide whether a verification is due."""
        now = _coerce_utc(now) or utcnow()
        record = self.ensure_device(device_id)
        previous = record.status

        if record.last_liveness_at is None:
            if record.consecutive_device_failures >= DEVICE_FAILURE_THRESHOLD:
                record.status = STATUS_UNAVAILABLE
                record.reason = "never_seen_and_repeated_000201"
                if record.next_verification_at is None:
                    record.next_verification_at = now + UNAVAILABLE_RECHECK_AFTER
            elif record.status != STATUS_UNKNOWN:
                record.status = STATUS_UNKNOWN
                record.reason = "no_valid_state"
            due = record.next_verification_at is None or now >= record.next_verification_at
            return Evaluation(previous != record.status, due, record.reason)

        stale_after, threshold_source = record.stale_after()
        stale = now - record.last_liveness_at > stale_after

        if stale:
            if record.consecutive_device_failures >= DEVICE_FAILURE_THRESHOLD:
                record.status = STATUS_UNAVAILABLE
                record.reason = "stale_and_repeated_000201"
                if record.next_verification_at is None:
                    record.next_verification_at = now + UNAVAILABLE_RECHECK_AFTER
            else:
                record.status = STATUS_SUSPECT
                record.reason = f"stale_{threshold_source}"
                if record.next_verification_at is None:
                    record.next_verification_at = now
        elif record.status == STATUS_UNAVAILABLE:
            # Do not recover solely because a clock/threshold calculation says
            # the prior liveness is fresh.  Recovery requires fresh positive
            # evidence (MQTT, successful getState, or successful command).
            pass
        elif record.status == STATUS_UNKNOWN:
            record.status = STATUS_AVAILABLE
            record.reason = "valid_liveness"

        should_verify = (
            record.status in {STATUS_SUSPECT, STATUS_UNAVAILABLE, STATUS_UNKNOWN}
            and (record.next_verification_at is None or now >= record.next_verification_at)
        )
        return Evaluation(previous != record.status, should_verify, record.reason)

    def should_verify(
        self,
        device_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether a normal background verification is still due."""
        return self.evaluate(device_id, now=now).should_verify

    def diagnostics(self) -> dict[str, Any]:
        """Return compact, read-only availability diagnostics."""
        output: dict[str, Any] = {}
        now = utcnow()
        for device_id, record in sorted(self._devices.items()):
            stale_after, threshold_source = record.stale_after()
            samples = list(record.mqtt_intervals_seconds)
            med = median(samples) if samples else None
            deviations = [abs(sample - med) for sample in samples] if med else []
            mad = median(deviations) if deviations else None
            mad_ratio = (mad / med) if med and mad is not None else None
            liveness_age = (
                (now - record.last_liveness_at).total_seconds()
                if record.last_liveness_at is not None
                else None
            )
            output[device_id] = {
                "name": record.name,
                "device_type": record.device_type,
                "model": record.model,
                "status": record.status,
                "ha_available": record.is_available(),
                "reason": record.reason,
                "last_liveness_at": _iso(record.last_liveness_at),
                "last_liveness_age_seconds": round(liveness_age, 1)
                if liveness_age is not None
                else None,
                "last_mqtt_at": _iso(record.last_mqtt_at),
                "last_http_success_at": _iso(record.last_http_success_at),
                "last_command_success_at": _iso(record.last_command_success_at),
                "last_reported_at": _iso(record.last_reported_at),
                "raw_online_hint": record.raw_online_hint,
                "stale_after_seconds": round(stale_after.total_seconds(), 1),
                "stale_threshold_source": threshold_source,
                "mqtt_interval_sample_count": len(samples),
                "mqtt_interval_median_seconds": round(med, 1) if med else None,
                "mqtt_interval_mad_ratio": round(mad_ratio, 3)
                if mad_ratio is not None
                else None,
                "consecutive_device_failures": record.consecutive_device_failures,
                "next_verification_at": _iso(record.next_verification_at),
                "last_error_code": record.last_error_code,
                "last_error": record.last_error,
                "last_error_source": record.last_error_source,
                "verification_requests": record.verification_requests,
                "verification_successes": record.verification_successes,
                "verification_unreachable": record.verification_unreachable,
                "command_successes": record.command_successes,
                "command_failures": record.command_failures,
            }
        return output
