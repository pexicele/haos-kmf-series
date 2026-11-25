"""Number platform for the KM-F Series integration.

One entity, the preset battery capacity, written with function 28 of the
Juntek protocol. The value on the meter is in tenths of an Ah and takes whole
Ah between 1 and 9999, which is exactly what a number entity in box mode
offers: a text box where Home Assistant rejects anything outside that range
before the value is ever sent.

The entity shows what the meter reports in the ``:A=`` frame rather than an
optimistic local echo, so a write the module refused never leaves a capacity
behind that the hardware does not have.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_NAME,
    DEFAULT_NAME,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    MAX_CAPACITY,
    MIN_CAPACITY,
    UNIT_AMPERE_HOUR,
)
from .coordinator import KmfcCoordinator

_LOGGER = logging.getLogger(__name__)

CAPACITY_KEY = "set_capacity"

DESCRIPTION = NumberEntityDescription(
    key=CAPACITY_KEY,
    translation_key=CAPACITY_KEY,
    native_min_value=float(MIN_CAPACITY),
    native_max_value=float(MAX_CAPACITY),
    native_step=1.0,
    native_unit_of_measurement=UNIT_AMPERE_HOUR,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the capacity number for this config entry."""
    coordinator: KmfcCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KmfcCapacityNumber(coordinator, entry)])


class KmfcCapacityNumber(CoordinatorEntity[KmfcCoordinator], NumberEntity):
    """The preset battery capacity, writable from the dashboard."""

    entity_description = DESCRIPTION
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: KmfcCoordinator, entry: ConfigEntry
    ) -> None:
        """Bind the entity to the device."""
        super().__init__(coordinator)
        host = coordinator.host
        self._attr_unique_id = f"{host}_{CAPACITY_KEY}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, host)},
            name=entry.data.get(CONF_NAME) or DEFAULT_NAME,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=f"http://{host}/",
        )

    @property
    def available(self) -> bool:
        """Available when the stream is up and a frame has been parsed."""
        return bool(self.coordinator.available and self.coordinator.data)

    @property
    def native_value(self) -> float | None:
        """Capacity stored in the meter, rounded to the whole Ah it holds."""
        if not self.coordinator.data:
            return None
        reported = self.coordinator.data.get("reported_capacity")
        if reported is None:
            return None
        return float(round(reported))

    async def async_set_native_value(self, value: float) -> None:
        """Write the capacity, then show what the meter reports back.

        The coordinator raises a HomeAssistantError when the module does not
        confirm, which Home Assistant surfaces as a failed action. Either way
        the state is rewritten from the newest frame, so a refused write
        leaves the old capacity on screen instead of a value the hardware
        does not have.
        """
        if not float(value).is_integer():
            raise HomeAssistantError(
                f"Capacity must be a whole number of {UNIT_AMPERE_HOUR}, "
                f"got {value:g}"
            )
        capacity = int(value)
        if not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
            raise HomeAssistantError(
                f"Capacity must be between {MIN_CAPACITY} and "
                f"{MAX_CAPACITY} Ah"
            )
        try:
            await self.coordinator.async_set_capacity(capacity)
        finally:
            self.async_write_ha_state()
