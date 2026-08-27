"""Data coordinator for YoLink Local integration."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable, Coroutine
from time import monotonic
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    ApiError,
    Device,
    DeviceEvent,
    TokenManager,
    YoLinkClient,
    YoLinkMQTTClient,
)
from .availability import AvailabilityManager
from .device_events import parse_ys5708_button_event
from .event_capture import DiagnosticEventCapture
from .outlet_power import (
    OutletPowerTracker,
    power_field,
    relay_is_on,
    sanitize_outlet_power_payload,
)
from .const import (
    DEVICE_DISCOVERY_INTERVAL,
    DOMAIN,
    HEALTH_EVALUATION_INTERVAL,
    YOLINK_EVENT,
)

_LOGGER = logging.getLogger(__name__)
type DeviceRegistryListener = Callable[[list[Device], list[Device]], None]

TRANSIENT_DEVICE_UNREACHABLE = "000201"
SET_STATE_TRANSPORT_RETRY_DELAY = 2.0
SET_STATE_DEVICE_RETRY_DELAY = 2.0
SET_STATE_ATTEMPTS = 2
HUB_HEALTH_FAILURE_THRESHOLD = 2
MQTT_DUPLICATE_WINDOW = 300.0
MQTT_DUPLICATE_CACHE_LIMIT = 256
BOOTSTRAP_CONCURRENCY = 2
DEVICE_IO_TIMEOUT = 10.0
VERIFY_JITTER_MIN = 0.25
VERIFY_JITTER_MAX = 1.25
COMMAND_CONFIRM_DELAY = 2.0


class YoLocalCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for YoLink Local devices.

    MQTT is the normal authoritative state path.  HTTP getState is used for the
    initial bootstrap and for targeted verification only when a device becomes
    stale/suspect or a command needs confirmation.  A transient ``000201`` is
    therefore treated as one failed request, not as proof that the device is
    offline.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: YoLinkClient,
        token_manager: TokenManager,
        session: aiohttp.ClientSession,
        config_entry_id: str,
        net_id: str,
        mqtt_port: int = 18080,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="YoLink Local",
            update_interval=None,
        )
        self._client = client
        self._token_manager = token_manager
        self._session = session
        self._config_entry_id = config_entry_id
        self._net_id = net_id
        self._mqtt_port = mqtt_port

        self._mqtt_clients: dict[str, YoLinkMQTTClient] = {}
        self._devices: dict[str, Device] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._availability = AvailabilityManager()
        self._diagnostic_event_capture = DiagnosticEventCapture()
        self._outlet_power = OutletPowerTracker()

        self._reconnect_task: asyncio.Task[None] | None = None
        self._discovery_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._verification_task: asyncio.Task[None] | None = None
        self._command_confirmation_tasks: dict[str, asyncio.Task[Any]] = {}

        self._verification_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued_verifications: set[str] = set()
        self._queued_request_details: dict[str, dict[str, Any]] = {}
        self._device_io_lock = asyncio.Lock()

        self._device_registry_listeners: list[DeviceRegistryListener] = []
        self._shutdown = False
        self._hub_health_failures = 0
        self._hub_api_healthy = True
        self._recent_mqtt_events: dict[str, float] = {}

    @property
    def devices(self) -> dict[str, Device]:
        """Return the device registry."""
        return self._devices

    def register_device_registry_listener(
        self, listener: DeviceRegistryListener
    ) -> Callable[[], None]:
        """Subscribe to device registry changes."""
        self._device_registry_listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._device_registry_listeners:
                self._device_registry_listeners.remove(listener)

        return unsubscribe

    def _create_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[None] | Any:
        """Create a background task without blocking HA startup."""
        if hasattr(self.hass, "async_create_background_task"):
            return self.hass.async_create_background_task(coro, name)
        return self.hass.async_create_task(coro)

    def _register_device_health(self, device: Device) -> None:
        """Ensure availability and telemetry metadata exist for a discovered device."""
        self._availability.ensure_device(
            device.device_id,
            name=device.name,
            device_type=device.device_type,
            model=device.model,
        )
        if device.device_type == "Outlet":
            self._outlet_power.ensure_device(
                device.device_id,
                name=device.name,
                model=device.model,
            )

    async def _async_setup(self) -> None:
        """Set up the coordinator: fetch devices and start MQTT/background work."""
        devices = await self._client.get_devices()
        self._devices = {d.device_id: d for d in devices}
        for device in devices:
            self._register_device_health(device)
        self._remove_stale_registry_devices(set(self._devices))

        # MQTT connects in the background exactly as in the upstream fork so HA
        # startup is not blocked on broker connection/reconnection.
        self._on_mqtt_disconnect()
        if self._discovery_task is None:
            self._discovery_task = self._create_background_task(
                self._async_device_discovery_loop(),
                "yolocal_device_discovery",
            )
        if self._health_task is None:
            self._health_task = self._create_background_task(
                self._async_health_loop(),
                "yolocal_health",
            )
        if self._verification_task is None:
            self._verification_task = self._create_background_task(
                self._async_verification_worker(),
                "yolocal_verification",
            )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Bootstrap state for devices that do not yet have a valid cached state.

        The old implementation queried every device concurrently every five
        minutes.  This method now runs as the coordinator's startup bootstrap
        and only queries devices that lack state.  Bootstrap concurrency is
        deliberately bounded.  A transient device-specific bootstrap failure is
        queued for one serialized targeted verification immediately after the
        bootstrap batch completes, rather than leaving the entity unavailable
        until the periodic health loop runs.
        """
        missing = [
            (device_id, device)
            for device_id, device in self._devices.items()
            if not self._states.get(device_id)
        ]
        if not missing:
            return self._states.copy()

        semaphore = asyncio.Semaphore(BOOTSTRAP_CONCURRENCY)

        async def fetch_one(
            device_id: str, device: Device
        ) -> tuple[str, dict[str, Any] | None, bool]:
            async with semaphore:
                try:
                    async with asyncio.timeout(DEVICE_IO_TIMEOUT):
                        state = await self._client.get_state(device)
                except ApiError as err:
                    if err.code == TRANSIENT_DEVICE_UNREACHABLE:
                        self._availability.record_device_unreachable(
                            device_id,
                            error_code=err.code,
                            error=str(err),
                            source="bootstrap",
                        )
                        _LOGGER.debug(
                            "Initial state temporarily unavailable for %s: %s",
                            device.name,
                            err,
                        )
                        return device_id, None, True
                    _LOGGER.warning(
                        "Failed to get initial state for %s: %s",
                        device.name,
                        err,
                    )
                    return device_id, None, False
                except (aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.warning(
                        "Transport failure getting initial state for %s: %s",
                        device.name,
                        err,
                    )
                    return device_id, None, False
                except Exception:
                    _LOGGER.warning(
                        "Failed to get initial state for %s",
                        device.name,
                        exc_info=True,
                    )
                    return device_id, None, False

                normalized = self._normalize_http_state(state, device)
                self._availability.record_http_success(
                    device_id,
                    reported_at=self._reported_at(normalized),
                    raw_online=self._raw_online(normalized),
                    source="bootstrap",
                )
                return device_id, normalized, False

        results = await asyncio.gather(*(fetch_one(*item) for item in missing))
        refreshed_states = self._states.copy()
        bootstrap_retry_ids: list[str] = []
        for device_id, incoming_state, retry_transient in results:
            if incoming_state is None:
                if retry_transient:
                    bootstrap_retry_ids.append(device_id)
                continue
            refreshed_states[device_id] = self._with_derived_online(
                device_id,
                self._merge_state_payload(
                    refreshed_states.get(device_id, {}),
                    incoming_state,
                ),
            )

        self._states = refreshed_states

        # One transient startup 000201 should not leave an entity waiting for
        # the periodic health loop. Queue one forced targeted read after the
        # bootstrap batch so the existing worker can serialize it safely.
        for device_id in bootstrap_retry_ids:
            self._queue_verification(
                device_id,
                reason="bootstrap_transient_retry",
                force=True,
            )

        return refreshed_states.copy()

    async def _fetch_all_states(self) -> None:
        """Compatibility helper: bootstrap only states that are still missing."""
        self._states = await self._async_update_data()

    async def _async_get_state_with_retry(
        self,
        device: Device,
        attempts: int = 3,
    ) -> dict[str, Any] | None:
        """Compatibility helper for targeted reads without poisoning availability.

        This method is intentionally *not* used by a periodic all-device loop.
        Repeated ``000201`` returns ``None`` and leaves cached state untouched.
        """
        for attempt in range(1, attempts + 1):
            try:
                return await self._async_get_state_runtime(device)
            except ApiError as err:
                if err.code != TRANSIENT_DEVICE_UNREACHABLE:
                    raise
                if attempt < attempts:
                    await asyncio.sleep(SET_STATE_DEVICE_RETRY_DELAY)
                    continue
                return None
        return None

    async def _async_get_state_runtime(self, device: Device) -> dict[str, Any]:
        """Perform one serialized runtime getState request."""
        async with self._device_io_lock:
            async with asyncio.timeout(DEVICE_IO_TIMEOUT):
                return await self._client.get_state(device)

    async def _async_set_state_runtime(
        self, device: Device, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Perform one serialized runtime setState request."""
        async with self._device_io_lock:
            async with asyncio.timeout(DEVICE_IO_TIMEOUT):
                return await self._client.set_state(device, params)

    async def _async_set_state_with_retry(
        self,
        device: Device,
        params: dict[str, Any],
        attempts: int = SET_STATE_ATTEMPTS,
    ) -> dict[str, Any]:
        """Set device state, retrying transient 000201 without changing availability."""
        for attempt in range(1, attempts + 1):
            try:
                result = await self._async_set_state_runtime(device, params)
                self._mark_hub_api_available()
                availability_changed = self._availability.record_command_success(
                    device.device_id
                )
                self._notify_if_ha_availability_changed(
                    device.device_id, availability_changed
                )
                return result
            except ApiError as err:
                if err.code != TRANSIENT_DEVICE_UNREACHABLE:
                    raise
                if attempt < attempts:
                    await asyncio.sleep(SET_STATE_DEVICE_RETRY_DELAY)
                    continue
                availability_changed = self._availability.record_command_failure(
                    device.device_id,
                    error_code=err.code,
                    error=str(err),
                )
                self._notify_if_ha_availability_changed(
                    device.device_id, availability_changed
                )
                # Important: command failure is surfaced to the caller, but the
                # cached device state is NOT changed to online:false.
                raise
        raise RuntimeError("Failed to set device state")

    async def _async_send_command_with_transport_recovery(
        self,
        device: Device,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a command, recovering one ambiguous transport failure."""
        try:
            return await self._async_set_state_with_retry(device, params)
        except (aiohttp.ClientConnectionError, TimeoutError) as err:
            self._mark_hub_api_failure(err)
            availability_changed = self._availability.record_transport_error(
                device.device_id,
                error=str(err),
                source="command_transport",
            )
            self._notify_if_ha_availability_changed(
                device.device_id, availability_changed
            )
            if hasattr(self._client, "switch_host"):
                self._client.switch_host()
            await asyncio.sleep(SET_STATE_TRANSPORT_RETRY_DELAY)

            # The command response may have been lost even if the command
            # reached the device.  Verify before issuing a duplicate command.
            try:
                state = await self._async_get_state_runtime(device)
                normalized_state = self._normalize_http_state(state, device)
                self._availability.record_http_success(
                    device.device_id,
                    reported_at=self._reported_at(normalized_state),
                    raw_online=self._raw_online(normalized_state),
                    source="command_transport_verify",
                )
                self._update_device_state(
                    device.device_id,
                    self._merge_state_payload(
                        self._states.get(device.device_id, {}),
                        normalized_state,
                    ),
                )
                if self._state_matches_command(normalized_state, params):
                    return {}
            except ApiError as verify_err:
                if verify_err.code == TRANSIENT_DEVICE_UNREACHABLE:
                    changed = self._availability.record_device_unreachable(
                        device.device_id,
                        error_code=verify_err.code,
                        error=str(verify_err),
                        source="command_transport_verify",
                    )
                    self._notify_if_ha_availability_changed(device.device_id, changed)
                else:
                    _LOGGER.debug(
                        "API error verifying state after transport error for %s",
                        device.name,
                        exc_info=True,
                    )
            except Exception:
                _LOGGER.debug(
                    "Failed to verify state after transport error for %s",
                    device.name,
                    exc_info=True,
                )

            try:
                return await self._async_set_state_with_retry(
                    device, params, attempts=1
                )
            except (aiohttp.ClientConnectionError, TimeoutError):
                raise err

    def _state_matches_command(
        self,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        """Return True when current state already reflects the command params."""
        for key, expected_value in params.items():
            if not self._value_matches_command(
                self._state_command_value(state, key),
                expected_value,
            ):
                return False
        return True

    def _state_command_value(self, state: dict[str, Any], key: str) -> Any:
        """Return a comparable state value for a command key."""
        nested_state = state.get("state")
        if isinstance(nested_state, dict) and key in nested_state:
            return nested_state.get(key)
        return state.get(key)

    def _value_matches_command(self, current_value: Any, expected_value: Any) -> bool:
        """Compare a current state value with a requested command value."""
        if isinstance(expected_value, dict):
            if not isinstance(current_value, dict):
                return False
            return all(
                self._value_matches_command(current_value.get(key), value)
                for key, value in expected_value.items()
            )
        if expected_value == "close":
            return current_value == "closed"
        return current_value == expected_value

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        self._shutdown = True
        for task_name in (
            "_discovery_task",
            "_health_task",
            "_verification_task",
            "_reconnect_task",
        ):
            task = getattr(self, task_name)
            if task:
                task.cancel()
                setattr(self, task_name, None)

        for task in list(self._command_confirmation_tasks.values()):
            task.cancel()
        self._command_confirmation_tasks.clear()

        for mqtt_client in list(self._mqtt_clients.values()):
            await mqtt_client.disconnect()
        self._mqtt_clients.clear()
        await self._session.close()

    async def _connect_mqtt(self) -> None:
        """Connect to all configured MQTT brokers."""
        connected_hosts: list[str] = []
        last_error: Exception | None = None
        for host in self._mqtt_hosts:
            try:
                await self._connect_mqtt_host(host)
            except Exception as err:
                last_error = err
                _LOGGER.warning(
                    "Failed to connect to YoLink MQTT broker at %s",
                    host,
                    exc_info=True,
                )
            else:
                connected_hosts.append(host)
        if not connected_hosts:
            if last_error is not None:
                raise last_error
            raise ConnectionError("No YoLink MQTT hosts configured")

    @property
    def _mqtt_hosts(self) -> tuple[str, ...]:
        """Return configured MQTT hosts."""
        hosts = getattr(self._client, "hosts", None)
        if hosts:
            return tuple(hosts)
        return (self._client.host,)

    async def _connect_mqtt_host(self, host: str) -> None:
        """Connect to one MQTT broker address."""
        if host in self._mqtt_clients:
            return
        if hasattr(self._token_manager, "get_token_for_host"):
            token = await self._token_manager.get_token_for_host(host)
        else:
            token = await self._token_manager.get_token()
        mqtt_client = YoLinkMQTTClient(
            host=host,
            net_id=self._net_id,
            client_id=self._token_manager.client_id,
            access_token=token,
            port=self._mqtt_port,
        )
        mqtt_client.subscribe(self._on_device_event)
        mqtt_client.on_disconnect(lambda host=host: self._on_mqtt_disconnect(host))
        try:
            await mqtt_client.connect()
            self._mqtt_clients[host] = mqtt_client
            _LOGGER.info("Connected to YoLink MQTT broker at %s", host)
            # If HTTP was also degraded, MQTT reconnect can restore the Local
            # path without waiting for the next discovery cycle.
            self.async_set_updated_data(self._states.copy())
        except Exception:
            try:
                await mqtt_client.disconnect()
            except Exception:
                _LOGGER.debug(
                    "Error while cleaning up failed MQTT client",
                    exc_info=True,
                )
            raise

    @callback
    def _on_device_event(self, event: DeviceEvent) -> None:
        """Handle a device event from MQTT."""
        device_id = event.device_id
        device = self._devices.get(device_id)
        if device is None:
            _LOGGER.debug("Ignoring event for unknown device: %s", device_id)
            return
        if self._is_duplicate_mqtt_event(event):
            _LOGGER.debug(
                "Ignoring duplicate MQTT event for %s: %s",
                device_id,
                event.event,
            )
            return

        # Keep a bounded RAM-only copy of relevant switch/dimmer/button-like events.
        # YS5708 button semantics are now validated and functional; retaining this
        # capture helps diagnose future device models without custom disk logging.
        self._diagnostic_event_capture.record(device, event)
        self._fire_local_device_event(device, event)

        event_data = event.data if isinstance(event.data, dict) else {}
        raw_online = self._raw_online(event_data)
        reported_at = self._reported_at(event_data)
        meaningful_payload_keys = set(event_data) - {
            "online", "reportAt", "lastReportedAt"
        }
        newer_report = self._availability.report_timestamp_is_newer(
            device_id, reported_at
        )

        if raw_online is False and not meaningful_payload_keys and not newer_report:
            # An offline-only hub hint is evidence to verify, not evidence that
            # the LoRa device itself is gone.  A newer device report timestamp
            # or an actual state payload still counts as positive liveness.
            self._availability.record_offline_hint(
                device_id,
                source="mqtt",
            )
        else:
            # A non-duplicate device report/state payload is positive liveness.
            # It immediately clears suspect/unavailable status.
            self._availability.record_mqtt_event(
                device_id,
                reported_at=reported_at,
                raw_online=raw_online,
            )

        normalized_event = self._normalize_mqtt_event(device, event_data)
        self._update_device_state(
            device_id,
            self._merge_state_payload(
                self._states.get(device_id, {}),
                normalized_event,
            ),
        )

    @callback
    def _fire_local_device_event(self, device: Device, event: DeviceEvent) -> None:
        """Fire a compact HA event for physical Local Hub device events.

        ``Switch.DevEvent`` is distinct from relay/state messages such as
        ``Switch.StatusChange`` and ``Switch.setState``.  The validated YS5708
        payload is translated into stable device-trigger types while the generic
        event still carries enough context for advanced automations.
        """
        if not str(event.event or "").endswith(".DevEvent"):
            return

        registry = dr.async_get(self.hass)
        registry_device = registry.async_get_device(
            identifiers={(DOMAIN, device.device_id)}
        )
        if registry_device is None:
            return

        event_data = event.data if isinstance(event.data, dict) else {}
        parsed = parse_ys5708_button_event(
            model=device.model,
            event_name=event.event,
            event_data=event_data,
        )

        bus_data: dict[str, Any] = {
            "device_id": registry_device.id,
            "source_device_id": device.device_id,
            "device_name": device.name,
            "device_type": device.device_type,
            "model": device.model,
            "event_name": event.event,
            "type": parsed.trigger_type if parsed else "device_event",
            "event_data": event_data,
        }
        if parsed is not None:
            bus_data.update(
                {
                    "button": parsed.button,
                    "key_mask": parsed.button,
                    "press_type": parsed.press_type,
                }
            )

        self.hass.bus.async_fire(YOLINK_EVENT, bus_data)

    def _is_duplicate_mqtt_event(self, event: DeviceEvent) -> bool:
        """Return True when the same MQTT event was recently processed."""
        now = monotonic()
        cutoff = now - MQTT_DUPLICATE_WINDOW
        for key, seen_at in list(self._recent_mqtt_events.items()):
            if seen_at < cutoff:
                self._recent_mqtt_events.pop(key, None)
        event_key = self._mqtt_event_key(event)
        if event_key in self._recent_mqtt_events:
            self._recent_mqtt_events[event_key] = now
            return True

        self._recent_mqtt_events[event_key] = now
        if len(self._recent_mqtt_events) > MQTT_DUPLICATE_CACHE_LIMIT:
            oldest_key = min(self._recent_mqtt_events, key=self._recent_mqtt_events.get)
            self._recent_mqtt_events.pop(oldest_key, None)
        return False

    def _mqtt_event_key(self, event: DeviceEvent) -> str:
        """Return a stable key for duplicate MQTT event detection."""
        try:
            raw = json.dumps(event.raw, sort_keys=True, separators=(",", ":"))
        except TypeError:
            raw = repr(event.raw)
        return f"{event.device_id}|{event.event}|{raw}"

    def _update_device_state(self, device_id: str, state: dict[str, Any]) -> None:
        """Store updated device state and notify listeners."""
        self._states[device_id] = self._with_derived_online(device_id, state)
        self.async_set_updated_data(self._states.copy())

    def _with_derived_online(
        self, device_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        """Return state with HA-facing online derived from the health manager."""
        derived = dict(state)
        derived["online"] = self.is_device_available(device_id)
        return derived

    def _sync_derived_online(self, device_id: str) -> bool:
        """Synchronize cached online with derived availability."""
        state = self._states.get(device_id)
        if not state:
            return False
        desired = self.is_device_available(device_id)
        if state.get("online") is desired:
            return False
        self._states[device_id] = {**state, "online": desired}
        return True

    def _notify_if_ha_availability_changed(
        self, device_id: str, status_changed: bool
    ) -> None:
        """Notify HA only when the derived availability actually changes."""
        if not status_changed:
            return
        if self._sync_derived_online(device_id):
            self.async_set_updated_data(self._states.copy())

    def _normalize_http_state(
        self, state: dict[str, Any], device: Device | None = None
    ) -> dict[str, Any]:
        """Normalize an HTTP getState payload to the coordinator's canonical shape."""
        normalized_state = self._sanitize_state_payload(state)
        if device is not None and device.device_type == "Outlet":
            # Observe the actual incoming sample before merge.  Null/malformed
            # power must never erase the last valid measurement.
            self._outlet_power.observe_payload(device.device_id, normalized_state)
            normalized_state = sanitize_outlet_power_payload(normalized_state)
        if normalized_state.get("reportAt") and "lastReportedAt" not in normalized_state:
            normalized_state["lastReportedAt"] = normalized_state["reportAt"]
        return normalized_state

    @staticmethod
    def _reported_at(state: dict[str, Any]) -> Any:
        """Return the best report timestamp exposed by a payload."""
        return state.get("lastReportedAt") or state.get("reportAt")

    @staticmethod
    def _raw_online(state: dict[str, Any]) -> bool | None:
        """Return a raw boolean online hint, if present."""
        value = state.get("online")
        return value if isinstance(value, bool) else None

    def _normalize_mqtt_event(
        self,
        device: Device,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize a flat MQTT event into the nested HTTP-like state shape."""
        normalized: dict[str, Any] = {}
        source_data = event_data

        if device.device_type == "Outlet":
            previous_relay_on = relay_is_on(self._states.get(device.device_id, {}))
            incoming_relay_on = relay_is_on(event_data)
            had_power_sample = self._outlet_power.observe_payload(
                device.device_id, event_data
            )
            if (
                incoming_relay_on is True
                and previous_relay_on is not True
                and not had_power_sample
            ):
                # The prior off-state zero is no longer a current load
                # measurement.  Let the next health pass request one targeted
                # serialized getState if MQTT does not provide power first.
                self._outlet_power.mark_on_without_measurement(device.device_id)
            source_data = sanitize_outlet_power_payload(event_data)

        for key in ("online", "reportAt", "lastReportedAt"):
            if key in source_data:
                normalized[key] = source_data[key]
        nested_state: dict[str, Any] = {}
        event_state = source_data.get("state")
        if isinstance(event_state, dict):
            nested_state.update(event_state)
        elif event_state is not None:
            nested_state["state"] = event_state
        for key, value in source_data.items():
            if key in {"state", "online", "reportAt", "lastReportedAt"}:
                continue
            if (
                device.device_type == "THSensor"
                and value is None
                and key in {"temperature", "humidity", "mode", "version"}
            ):
                continue
            nested_state[key] = value

        if nested_state:
            normalized["state"] = nested_state
        return self._sanitize_state_payload(normalized)

    def _merge_state_payload(
        self,
        existing_state: dict[str, Any],
        incoming_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge canonical state payloads while preserving nested HTTP shape."""
        merged_state = self._sanitize_state_payload({**existing_state, **incoming_state})
        merged_nested_state = self._merge_nested_state(
            existing_state.get("state"),
            incoming_state.get("state"),
        )
        if isinstance(merged_nested_state, dict):
            merged_state["state"] = self._sanitize_nested_state(merged_nested_state)
        elif merged_nested_state is not None:
            merged_state["state"] = merged_nested_state
        return merged_state

    def _merge_device_state(
        self,
        device_id: str,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a raw device event into the cached state shape."""
        device = self._devices[device_id]
        normalized_event = self._normalize_mqtt_event(device, event_data)
        return self._merge_state_payload(
            self._states.get(device_id, {}),
            normalized_event,
        )

    def _merge_thsensor_state(
        self,
        device_id: str,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a raw THSensor event into the cached state shape."""
        return self._merge_device_state(device_id, event_data)

    def _sanitize_state_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        """Remove inaccurate fields from a state payload."""
        sanitized = dict(state)
        sanitized.pop("batteryType", None)
        nested_state = sanitized.get("state")
        if isinstance(nested_state, dict):
            sanitized["state"] = self._sanitize_nested_state(nested_state)
        return sanitized

    def _sanitize_nested_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Remove inaccurate fields from a nested state object."""
        sanitized = dict(state)
        sanitized.pop("batteryType", None)
        return sanitized

    def _merge_nested_state(
        self,
        existing_state: Any,
        event_state: Any,
    ) -> Any | None:
        """Merge the payload's nested ``state`` field while preserving details."""
        if event_state is None:
            return None
        if isinstance(event_state, dict):
            if isinstance(existing_state, dict):
                return {**existing_state, **event_state}
            return event_state
        if isinstance(existing_state, dict):
            return {**existing_state, "state": event_state}
        return event_state

    @callback
    def _on_mqtt_disconnect(self, host: str | None = None) -> None:
        """Reconnect MQTT when the broker disconnects."""
        if self._shutdown:
            return
        if host is not None:
            self._mqtt_clients.pop(host, None)
            # Re-evaluate entity availability if the last working MQTT path
            # disappeared while the HTTP path is also unhealthy.
            self.async_set_updated_data(self._states.copy())
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(self._async_reconnect_mqtt())

    async def _async_reconnect_mqtt(self) -> None:
        """Reconnect missing MQTT clients with backoff."""
        backoff_seconds = 5
        while not self._shutdown:
            missing_hosts = [
                host for host in self._mqtt_hosts if host not in self._mqtt_clients
            ]
            if not missing_hosts:
                return
            for host in missing_hosts:
                try:
                    await self._connect_mqtt_host(host)
                except Exception:
                    _LOGGER.warning(
                        "Failed to reconnect YoLink MQTT broker at %s",
                        host,
                        exc_info=True,
                    )

            if all(host in self._mqtt_clients for host in self._mqtt_hosts):
                return
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 300)

    async def _async_device_discovery_loop(self) -> None:
        """Periodically check for device additions/removals."""
        interval = DEVICE_DISCOVERY_INTERVAL.total_seconds()
        while not self._shutdown:
            await asyncio.sleep(interval)
            await self._async_refresh_devices()

    async def _async_health_loop(self) -> None:
        """Evaluate liveness and telemetry freshness without broad device polling."""
        interval = HEALTH_EVALUATION_INTERVAL.total_seconds()
        while not self._shutdown:
            await asyncio.sleep(interval)
            notify = False
            for device_id, device in list(self._devices.items()):
                before_available = self._availability.is_available(device_id)
                evaluation = self._availability.evaluate(device_id)
                after_available = self._availability.is_available(device_id)
                if before_available != after_available:
                    self._sync_derived_online(device_id)
                    notify = True
                if evaluation.should_verify:
                    self._queue_verification(
                        device_id,
                        reason=evaluation.reason or "health_check",
                    )

                # YS6614-class outlet power is not guaranteed to be present in
                # sparse MQTT StatusChange reports.  Refresh only an ON outlet
                # whose active-power measurement itself is stale/missing.  This
                # is intentionally separate from device availability and uses
                # the same serialized I/O worker as health verification.
                if (
                    device.device_type == "Outlet"
                    and self._outlet_power.should_refresh(
                        device_id,
                        outlet_is_on=relay_is_on(self._states.get(device_id, {})),
                    )
                ):
                    self._queue_outlet_power_refresh(device_id)
            if notify:
                self.async_set_updated_data(self._states.copy())

    @callback
    def _queue_verification(
        self,
        device_id: str,
        *,
        reason: str,
        force: bool = False,
    ) -> None:
        """Queue or upgrade one serialized state request for health verification."""
        if self._shutdown or device_id not in self._devices:
            return
        details = self._queued_request_details.get(device_id)
        if details is not None:
            if not details.get("health"):
                self._availability.note_verification_request(device_id)
            details["health"] = True
            details["force"] = bool(details.get("force")) or force
            details.setdefault("reasons", set()).add(reason)
            return

        self._availability.note_verification_request(device_id)
        self._queued_request_details[device_id] = {
            "health": True,
            "telemetry": False,
            "force": force,
            "reasons": {reason},
        }
        self._queued_verifications.add(device_id)
        self._verification_queue.put_nowait(device_id)

    @callback
    def _queue_outlet_power_refresh(self, device_id: str) -> None:
        """Queue one targeted Outlet getState without affecting availability."""
        if self._shutdown or device_id not in self._devices:
            return
        details = self._queued_request_details.get(device_id)
        if details is not None:
            details["telemetry"] = True
            details.setdefault("reasons", set()).add("outlet_power_stale")
            return

        self._queued_request_details[device_id] = {
            "health": False,
            "telemetry": True,
            "force": False,
            "reasons": {"outlet_power_stale"},
        }
        self._queued_verifications.add(device_id)
        self._verification_queue.put_nowait(device_id)

    async def _async_verification_worker(self) -> None:
        """Run health/telemetry getState requests serially with de-duplication."""
        while not self._shutdown:
            device_id = await self._verification_queue.get()
            details = self._queued_request_details.get(device_id) or {
                "health": True,
                "telemetry": False,
                "force": False,
                "reasons": {"unknown"},
            }
            try:
                device = self._devices.get(device_id)
                if device is None:
                    continue

                health_requested = bool(details.get("health"))
                telemetry_requested = bool(details.get("telemetry"))
                force = bool(details.get("force"))
                reason = ",".join(sorted(details.get("reasons") or {"unknown"}))

                health_due = health_requested and (
                    force or self._availability.should_verify(device_id)
                )
                telemetry_due = telemetry_requested and (
                    device.device_type == "Outlet"
                    and self._outlet_power.should_refresh(
                        device_id,
                        outlet_is_on=relay_is_on(self._states.get(device_id, {})),
                    )
                )
                if not health_due and not telemetry_due:
                    continue

                if health_due and not force:
                    await asyncio.sleep(random.uniform(VERIFY_JITTER_MIN, VERIFY_JITTER_MAX))
                    # A fresh MQTT report/power sample may have arrived while
                    # queued or during jitter.  Re-check both reasons before IO.
                    health_due = self._availability.should_verify(device_id)
                    telemetry_due = telemetry_requested and (
                        device.device_type == "Outlet"
                        and self._outlet_power.should_refresh(
                            device_id,
                            outlet_is_on=relay_is_on(self._states.get(device_id, {})),
                        )
                    )
                    if not health_due and not telemetry_due:
                        continue

                if telemetry_due:
                    self._outlet_power.note_refresh_requested(device_id)

                try:
                    state = await self._async_get_state_runtime(device)
                except ApiError as err:
                    if telemetry_due:
                        self._outlet_power.note_refresh_failure(
                            device_id,
                            error=f"{err.code}: {err}",
                            unreachable=err.code == TRANSIENT_DEVICE_UNREACHABLE,
                        )
                    if health_due and err.code == TRANSIENT_DEVICE_UNREACHABLE:
                        before_available = self._availability.is_available(device_id)
                        self._availability.record_device_unreachable(
                            device_id,
                            error_code=err.code,
                            error=str(err),
                            source="verification",
                        )
                        after_available = self._availability.is_available(device_id)
                        if before_available != after_available:
                            self._sync_derived_online(device_id)
                            self.async_set_updated_data(self._states.copy())
                        _LOGGER.debug(
                            "Verification temporarily could not reach %s (%s): %s",
                            device.name,
                            reason,
                            err,
                        )
                    elif health_due:
                        _LOGGER.warning(
                            "Verification API error for %s (%s): %s",
                            device.name,
                            reason,
                            err,
                        )
                    else:
                        _LOGGER.debug(
                            "Outlet power refresh could not reach %s (%s): %s",
                            device.name,
                            reason,
                            err,
                        )
                    continue
                except (aiohttp.ClientError, TimeoutError) as err:
                    if telemetry_due:
                        self._outlet_power.note_refresh_failure(
                            device_id, error=str(err)
                        )
                    if health_due:
                        self._availability.record_transport_error(
                            device_id,
                            error=str(err),
                            source="verification_transport",
                        )
                    self._mark_hub_api_failure(err)
                    _LOGGER.warning(
                        "State refresh transport error for %s (%s): %s",
                        device.name,
                        reason,
                        err,
                    )
                    continue
                except Exception as err:
                    if telemetry_due:
                        self._outlet_power.note_refresh_failure(
                            device_id, error=str(err)
                        )
                    if health_due:
                        self._availability.record_transport_error(
                            device_id,
                            error=str(err),
                            source="verification_error",
                        )
                    _LOGGER.warning(
                        "State refresh failed for %s (%s)",
                        device.name,
                        reason,
                        exc_info=True,
                    )
                    continue

                self._mark_hub_api_available()
                normalized = self._normalize_http_state(state, device)
                self._availability.record_http_success(
                    device_id,
                    reported_at=self._reported_at(normalized),
                    raw_online=self._raw_online(normalized),
                    source="verification" if health_due else "outlet_power_refresh",
                )
                if telemetry_due:
                    present, valid_power = power_field(normalized)
                    self._outlet_power.note_refresh_success(
                        device_id,
                        had_power_measurement=(present and valid_power is not None)
                        or relay_is_on(normalized) is False,
                    )
                self._update_device_state(
                    device_id,
                    self._merge_state_payload(
                        self._states.get(device_id, {}),
                        normalized,
                    ),
                )
            finally:
                self._queued_request_details.pop(device_id, None)
                self._queued_verifications.discard(device_id)
                self._verification_queue.task_done()

    async def _async_refresh_devices(self) -> bool:
        """Refresh the device registry and notify listeners if membership changed."""
        try:
            devices = await self._client.get_devices()
        except Exception as err:
            _LOGGER.warning("Failed to refresh device list", exc_info=True)
            self._mark_hub_api_failure(err)
            return False
        self._mark_hub_api_available()
        new_devices = {device.device_id: device for device in devices}
        for device in devices:
            self._register_device_health(device)

        if set(new_devices) == set(self._devices):
            self._devices = new_devices
            return False

        existing_device_ids = set(self._devices)
        added_ids = sorted(set(new_devices) - existing_device_ids)
        removed_ids = sorted(existing_device_ids - set(new_devices))
        added_devices = [new_devices[device_id] for device_id in added_ids]
        removed_devices = [self._devices[device_id] for device_id in removed_ids]
        _LOGGER.info(
            "Device registry changed; added=%s removed=%s.",
            added_ids,
            removed_ids,
        )
        self._devices = new_devices
        for device_id in removed_ids:
            self._states.pop(device_id, None)
            self._availability.remove_device(device_id)
            self._outlet_power.remove_device(device_id)
            self._queued_request_details.pop(device_id, None)
            self._queued_verifications.discard(device_id)
            self._remove_device_from_registry(device_id)

        # New devices are uncommon, so one serialized state read is appropriate
        # here.  A transient 000201 leaves the new device unknown and the health
        # loop will retry later rather than falsely declaring it offline.
        for device in added_devices:
            try:
                state = await self._async_get_state_runtime(device)
                normalized = self._normalize_http_state(state, device)
                self._availability.record_http_success(
                    device.device_id,
                    reported_at=self._reported_at(normalized),
                    raw_online=self._raw_online(normalized),
                    source="discovery",
                )
                self._states[device.device_id] = self._with_derived_online(
                    device.device_id,
                    normalized,
                )
            except ApiError as err:
                if err.code == TRANSIENT_DEVICE_UNREACHABLE:
                    self._availability.record_device_unreachable(
                        device.device_id,
                        error_code=err.code,
                        error=str(err),
                        source="discovery",
                    )
                    _LOGGER.debug(
                        "New device state temporarily unavailable for %s: %s",
                        device.name,
                        err,
                    )
                else:
                    _LOGGER.warning("Failed to get initial state for %s", device.name)
                self._states[device.device_id] = {}
            except Exception:
                _LOGGER.warning("Failed to get initial state for %s", device.name)
                self._states[device.device_id] = {}

        for listener in list(self._device_registry_listeners):
            try:
                listener(added_devices, removed_devices)
            except Exception:
                _LOGGER.exception("Device registry listener failed")

        self.async_set_updated_data(self._states.copy())
        return True

    def _mark_hub_api_available(self) -> None:
        """Mark the HTTP/API transport healthy without changing device health."""
        was_healthy = self._hub_api_healthy
        self._hub_health_failures = 0
        self._hub_api_healthy = True
        if not self.last_update_success:
            self.async_set_updated_data(self._states.copy())
        elif not was_healthy:
            self.async_set_updated_data(self._states.copy())

    def _mark_hub_api_failure(self, err: Exception) -> None:
        """Mark HTTP/API transport degraded only after repeated transport failures."""
        self._hub_health_failures += 1
        if self._hub_health_failures >= HUB_HEALTH_FAILURE_THRESHOLD:
            was_healthy = self._hub_api_healthy
            self._hub_api_healthy = False
            self.async_set_update_error(err)
            if was_healthy:
                self.async_set_updated_data(self._states.copy())

    def _has_live_transport(self) -> bool:
        """Return True while either Local transport path is currently usable."""
        return bool(self._mqtt_clients) or self._hub_api_healthy

    def _remove_device_from_registry(self, device_id: str) -> None:
        """Remove a deleted device from the HA device registry."""
        self._remove_entity_registry_entries(device_id)
        registry = dr.async_get(self.hass)
        device_entry = registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if device_entry is not None:
            registry.async_remove_device(device_entry.id)

    def _remove_stale_registry_devices(self, active_device_ids: set[str]) -> None:
        """Remove registry devices that no longer exist on the hub."""
        registry = dr.async_get(self.hass)
        if hasattr(dr, "async_entries_for_config_entry"):
            device_entries = dr.async_entries_for_config_entry(
                registry,
                self._config_entry_id,
            )
        else:
            device_entries = list(getattr(registry, "devices", {}).values())
        for entry in list(device_entries):
            normalized_identifiers = set(getattr(entry, "identifiers", set()))
            if len(normalized_identifiers) != 1:
                continue
            domain, device_id = next(iter(normalized_identifiers))
            if domain != DOMAIN or device_id in active_device_ids:
                continue
            self._remove_entity_registry_entries(device_id)
            registry.async_remove_device(entry.id)

    def _remove_entity_registry_entries(self, device_id: str) -> None:
        """Remove orphaned entity-registry entries for a missing device."""
        registry = er.async_get(self.hass)
        if hasattr(er, "async_entries_for_config_entry"):
            entity_entries = er.async_entries_for_config_entry(
                registry,
                self._config_entry_id,
            )
        else:
            entity_entries = list(getattr(registry, "entities", {}).values())
        for entry in list(entity_entries):
            entity_id = getattr(entry, "entity_id", None)
            unique_id = getattr(entry, "unique_id", None)
            if unique_id == device_id or (
                isinstance(unique_id, str) and unique_id.startswith(f"{device_id}_")
            ):
                if entity_id is not None:
                    registry.async_remove(entity_id)

    def get_state(self, device_id: str) -> dict[str, Any]:
        """Get the current state for a device."""
        return self._states.get(device_id, {})

    def is_device_available(self, device_id: str) -> bool:
        """Return HA-facing availability from device health plus Local transport health.

        A failed device query never poisons hub health, and an HTTP outage never
        poisons device health.  Entities become unavailable for transport reasons
        only when *both* MQTT and HTTP/API paths are unavailable.
        """
        return self._has_live_transport() and self._availability.is_available(device_id)

    def availability_diagnostics(self) -> dict[str, Any]:
        """Return compact availability diagnostics."""
        return self._availability.diagnostics()

    def event_capture_diagnostics(self) -> dict[str, Any]:
        """Return bounded RAM-only MQTT event capture diagnostics."""
        return self._diagnostic_event_capture.diagnostics()

    def outlet_power_diagnostics(self) -> dict[str, Any]:
        """Return RAM-only Outlet active-power telemetry diagnostics."""
        return self._outlet_power.diagnostics()

    def runtime_diagnostics(self) -> dict[str, Any]:
        """Return coordinator runtime diagnostics without credentials/secrets."""
        return {
            "device_count": len(self._devices),
            "cached_state_count": len(self._states),
            "mqtt_configured_host_count": len(self._mqtt_hosts),
            "mqtt_connected_host_count": len(self._mqtt_clients),
            "verification_queue_depth": self._verification_queue.qsize(),
            "verification_devices_queued": len(self._queued_verifications),
            "hub_api_failure_count": self._hub_health_failures,
            "hub_api_healthy": self._hub_api_healthy,
            "mqtt_transport_healthy": bool(self._mqtt_clients),
            "local_transport_healthy": self._has_live_transport(),
            "coordinator_last_update_success": self.last_update_success,
            "diagnostic_event_capture_stored": self._diagnostic_event_capture.diagnostics()[
                "stored_event_count"
            ],
            "outlet_power_tracking_count": len(self._outlet_power.diagnostics()),
            "outlet_power_refresh_queue_shares_device_io_lock": True,
        }

    def _schedule_command_confirmation(
        self, device_id: str, params: dict[str, Any]
    ) -> None:
        """Confirm only the newest command for a device if MQTT stays silent."""
        previous = self._command_confirmation_tasks.pop(device_id, None)
        if previous is not None and not previous.done():
            previous.cancel()

        task = self.hass.async_create_task(
            self._async_confirm_command(device_id, dict(params))
        )
        self._command_confirmation_tasks[device_id] = task

        def _cleanup(done_task: asyncio.Task[Any]) -> None:
            if self._command_confirmation_tasks.get(device_id) is done_task:
                self._command_confirmation_tasks.pop(device_id, None)

        task.add_done_callback(_cleanup)

    async def _async_confirm_command(
        self, device_id: str, params: dict[str, Any]
    ) -> None:
        """Queue one read-only confirmation if MQTT did not reflect the command."""
        await asyncio.sleep(COMMAND_CONFIRM_DELAY)
        if self._shutdown or device_id not in self._devices:
            return
        if self._state_matches_command(self._states.get(device_id, {}), params):
            return
        self._queue_verification(
            device_id,
            reason="command_confirmation",
            force=True,
        )

    async def async_send_command(
        self, device_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a command and let MQTT confirm it when possible."""
        device = self._devices.get(device_id)
        if not device:
            raise ValueError(f"Unknown device: {device_id}")
        result = await self._async_send_command_with_transport_recovery(device, params)

        # Do not immediately issue getState after every successful command.
        # Most commands generate MQTT quickly; only fall back to one targeted
        # read if the cached state still does not match after a short delay.
        self._schedule_command_confirmation(device_id, params)
        return result


async def create_coordinator(
    hass: HomeAssistant,
    host: str,
    client_id: str,
    client_secret: str,
    config_entry_id: str,
    net_id: str,
    http_port: int = 1080,
    mqtt_port: int = 18080,
    hosts: list[str] | None = None,
) -> YoLocalCoordinator:
    """Create a coordinator.

    Home Assistant's first coordinator refresh performs setup and a bounded
    initial bootstrap.  Ongoing state is MQTT-first with targeted verification.

    Raises:
        AuthenticationError: If credentials are invalid.
        Exception: If setup fails.
    """
    session = aiohttp.ClientSession()
    try:
        token_manager = TokenManager(
            host,
            client_id,
            client_secret,
            session,
            http_port,
            hosts=hosts,
        )
        await token_manager.get_token()

        client = YoLinkClient(host, token_manager, session, http_port, hosts=hosts)
        return YoLocalCoordinator(
            hass, client, token_manager, session, config_entry_id, net_id, mqtt_port
        )
    except Exception:
        await session.close()
        raise
