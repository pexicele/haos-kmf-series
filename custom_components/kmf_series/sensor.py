"""Sensor platform for the KM-F Series integration.

Read only values. The preset battery capacity is writable too, that one lives
in the number platform, these two report what the meter says it holds.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_NAME,
    DEFAULT_NAME,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    UNIT_AMPERE_HOUR,
)
from .coordinator import KmfcCoordinator


@dataclass(frozen=True, kw_only=True)
class KmfcSensorDescription(SensorEntityDescription):
    """Describe one KM-F sensor, including how to read its value."""

    value_fn: Callable[[dict[str, Any]], Any] = lambda frame: None


SENSORS: tuple[KmfcSensorDescription, ...] = (
    KmfcSensorDescription(
        key="voltage",
        translation_key="voltage",
        value_fn=lambda frame: frame.get("voltage"),
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KmfcSensorDescription(
        key="current",
        translation_key="current",
        value_fn=lambda frame: frame.get("current"),
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KmfcSensorDescription(
        key="power",
        translation_key="power",
        value_fn=lambda frame: frame.get("power"),
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KmfcSensorDescription(
        key="soc",
        translation_key="state_of_charge",
        value_fn=lambda frame: frame.get("soc"),
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KmfcSensorDescription(
        key="remaining_capacity",
        translation_key="remaining_capacity",
        value_fn=lambda frame: frame.get("remaining_capacity"),
        native_unit_of_measurement=UNIT_AMPERE_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
    ),
    KmfcSensorDescription(
        key="reported_capacity",
        translation_key="reported_capacity",
        value_fn=lambda frame: frame.get("reported_capacity"),
        native_unit_of_measurement=UNIT_AMPERE_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    KmfcSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        value_fn=lambda frame: frame.get("time_remaining"),
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        entity_registry_enabled_default=False,
    ),
    KmfcSensorDescription(
        key="status",
        translation_key="status",
        value_fn=lambda frame: frame.get("status"),
        device_class=SensorDeviceClass.ENUM,
        options=["charging", "discharging", "idle"],
    ),
    KmfcSensorDescription(
        key="energy_charged",
        translation_key="energy_charged",
        value_fn=lambda frame: frame.get("energy_charged"),
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    KmfcSensorDescription(
        key="energy_discharged",
        translation_key="energy_discharged",
        value_fn=lambda frame: frame.get("energy_discharged"),
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per description for this config entry."""
    coordinator: KmfcCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        KmfcSensor(coordinator, description, entry) for description in SENSORS
    )


class KmfcSensor(CoordinatorEntity[KmfcCoordinator], SensorEntity):
    """One value of the KM-F frame, kept in sync by the coordinator."""

    entity_description: KmfcSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: KmfcCoordinator,
        description: KmfcSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        """Bind the description to this device."""
        super().__init__(coordinator)
        self.entity_description = description
        host = coordinator.host
        self._attr_unique_id = f"{host}_{description.key}"
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
    def native_value(self) -> Any:
        """Return the value for this sensor from the newest frame."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
