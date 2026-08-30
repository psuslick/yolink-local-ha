"""Diagnostics support for YoLink Local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HUB_IP,
    CONF_NET_ID,
    CONF_SECONDARY_HUB_IP,
    DOMAIN,
)
from .coordinator import YoLocalCoordinator

TO_REDACT = {
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HUB_IP,
    CONF_NET_ID,
    CONF_SECONDARY_HUB_IP,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted, read-only diagnostics for a config entry."""
    coordinator: YoLocalCoordinator = hass.data[DOMAIN][entry.entry_id]
    device_registry = dr.async_get(hass)

    # Diagnostic-only view of the exact names parsed from Home.getDeviceList,
    # alongside Home Assistant's current registry values. Device identifiers
    # are intentionally included so a renamed device can be matched
    # unambiguously without exposing credentials or tokens.
    device_names = []
    for device_id, device in sorted(coordinator.devices.items()):
        registry_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, device_id)}
        )
        device_names.append(
            {
                "device_id": device_id,
                "local_api_name": device.name,
                "device_type": device.device_type,
                "model": device.model,
                "ha_registry_name": (
                    registry_entry.name if registry_entry is not None else None
                ),
                "ha_name_by_user": (
                    registry_entry.name_by_user
                    if registry_entry is not None
                    else None
                ),
                "names_match": (
                    registry_entry is not None
                    and registry_entry.name == device.name
                ),
            }
        )

    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "runtime": coordinator.runtime_diagnostics(),
        "device_names": device_names,
        "availability": coordinator.availability_diagnostics(),
        "outlet_power": coordinator.outlet_power_diagnostics(),
        "mqtt_event_capture": coordinator.event_capture_diagnostics(),
    }
