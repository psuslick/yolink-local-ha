"""Diagnostics support for YoLink Local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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
    return {
        "config_entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "runtime": coordinator.runtime_diagnostics(),
        "availability": coordinator.availability_diagnostics(),
    }
