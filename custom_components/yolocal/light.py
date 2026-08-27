"""Light platform for YoLink Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import YoLocalCoordinator
from .dimmer import ha_brightness_to_yolink, yolink_brightness_to_ha
from .entity import YoLocalEntity, async_setup_device_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink dimmers from a config entry."""

    def build_entities(
        coordinator: YoLocalCoordinator,
        device,
    ) -> list[YoLocalDimmer]:
        if device.device_type != "Dimmer":
            return []
        return [YoLocalDimmer(coordinator, device)]

    await async_setup_device_entities(hass, entry, async_add_entities, build_entities)


class YoLocalDimmer(YoLocalEntity, LightEntity):
    """YoLink dimmer exposed as a native Home Assistant light."""

    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    @property
    def is_on(self) -> bool | None:
        """Return whether the dimmer relay is on."""
        state = self.state_value("state", fallback=True)
        if state is None:
            return None
        return state == "open"

    @property
    def brightness(self) -> int | None:
        """Return brightness on Home Assistant's 0..255 scale."""
        return yolink_brightness_to_ha(
            self.state_value("brightness", fallback=True)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the dimmer on, optionally setting brightness."""
        params: dict[str, Any] = {"state": "open"}
        if ATTR_BRIGHTNESS in kwargs:
            brightness = ha_brightness_to_yolink(kwargs[ATTR_BRIGHTNESS])
            if brightness is not None:
                # A HA brightness of 0 semantically means off. Avoid issuing an
                # invalid/open-at-zero command to the dimmer.
                if brightness <= 0:
                    await self.async_turn_off()
                    return
                params["brightness"] = brightness
        await self.coordinator.async_send_command(self._device.device_id, params)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the dimmer off without overwriting its remembered brightness."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": "closed"},
        )
