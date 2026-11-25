"""Minimal stand-ins for the Home Assistant modules the integration imports.

Only what the integration touches is faked, with the same signatures, so the
real integration code can be driven end to end in a plain Python run:

    python3 tests/test_integration.py

This file is a test helper, it is not part of the shipped integration.
"""

from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import typing

callback = lambda func: func  # noqa: E731 - decorator stand-in

_DataT = typing.TypeVar("_DataT")
_CoordinatorT = typing.TypeVar("_CoordinatorT")


# ---------------------------------------------------------------- core
class _ConfigEntries:
    """Route platform forwarding to the modules under test."""

    def __init__(self, modules: dict) -> None:
        self._modules = modules

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        for platform in platforms:
            module = self._modules[str(platform)]
            await module.async_setup_entry(_HASS, entry, _collect(entry))

    async def async_unload_platforms(self, entry, platforms) -> bool:
        return True


class _Harness:
    """The sliver of HomeAssistant the integration uses."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.config_entries = _ConfigEntries({})
        self.entity_registry = EntityRegistry()
        self.is_stopping = False

    def async_create_task(self, target, name=None, eager_start=True):
        return asyncio.ensure_future(target)


_HASS: _Harness | None = None


class HomeAssistant(_Harness):
    """Fake hass instance."""


# ---------------------------------------------------------------- const
class _Enum:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


PERCENTAGE = "%"


class EntityCategory:
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class UnitOfTime:
    MINUTES = "min"
    HOURS = "h"


class UnitOfElectricCurrent:
    AMPERE = "A"


class UnitOfElectricPotential:
    VOLT = "V"


class UnitOfEnergy:
    KILO_WATT_HOUR = "kWh"


class UnitOfPower:
    WATT = "W"


class Platform:
    SENSOR = "sensor"
    NUMBER = "number"


# ---------------------------------------------------------------- exceptions
class HomeAssistantError(Exception):
    pass


class IntegrationError(HomeAssistantError):
    pass


class ConfigEntryError(IntegrationError):
    pass


class ConfigEntryNotReady(IntegrationError):
    pass


class ConfigEntryAuthFailed(IntegrationError):
    pass


# ------------------------------------------------------- config_entries
class ConfigEntry(typing.Generic[_DataT]):
    """Enough of a config entry for setup and unload."""

    def __init__(self, data: dict, options: dict | None = None) -> None:
        self.entry_id = "test-entry"
        self.data = data
        self.options = options or {}
        self._update_listeners: list = []
        self._on_unload: list = []
        self.runtime_data: typing.Any = None

    def add_update_listener(self, listener):
        self._update_listeners.append(listener)

        def unsubscribe() -> None:
            self._update_listeners.remove(listener)

        return unsubscribe

    def async_on_unload(self, func) -> None:
        self._on_unload.append(func)


ConfigFlowResult = dict


class _FlowHandler:
    """Stand-in for ConfigFlow / OptionsFlow base classes."""

    def __init__(self) -> None:
        self.hass = _HASS

    async def async_set_unique_id(self, unique_id, **kwargs) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, **kwargs) -> None:
        return None

    def async_show_form(self, **kwargs) -> dict:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs) -> dict:
        return {"type": "create_entry", **kwargs}

    async def async_step_init(self, user_input=None):  # pragma: no cover
        raise NotImplementedError


class ConfigFlow(_FlowHandler):
    """ConfigFlow stand-in accepting the domain keyword."""

    def __init_subclass__(cls, domain=None, **kwargs) -> None:
        cls.domain = domain
        super().__init_subclass__(**kwargs)

    def async_release_memory_usage(self) -> None:
        return None


class OptionsFlow(_FlowHandler):
    """OptionsFlow stand-in exposing config_entry."""

    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        super().__init__()
        self.config_entry = config_entry


# ---------------------------------------------------------------- helpers
class UpdateFailed(HomeAssistantError):
    pass


class DataUpdateCoordinator(typing.Generic[_DataT]):
    """The coordinator behaviour the integration relies on."""

    def __init__(
        self,
        hass,
        logger,
        *,
        config_entry=None,
        name=None,
        update_interval=None,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.name = name
        self.update_interval = update_interval
        self.data: typing.Any = None
        self.last_update_success = True
        self._listeners: list = []
        self.refresh_calls = 0

    async def async_config_entry_first_refresh(self) -> None:
        await self._async_setup()
        await self._async_refresh()

    async def _async_setup(self) -> None:
        return None

    async def _async_refresh(self) -> None:
        self.refresh_calls += 1
        try:
            self.data = await self._async_update_data()
        except UpdateFailed as err:
            self.last_update_success = False
            raise ConfigEntryNotReady(str(err)) from err
        self.last_update_success = True
        # HA hands the new data to the entities once a refresh succeeds.
        self.async_update_listeners()

    async def _async_update_data(self):  # pragma: no cover
        raise NotImplementedError

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    def async_update_listeners(self) -> None:
        for listener in self._listeners:
            listener()

    def async_add_listener(self, listener, context=None) -> callable:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def async_request_refresh(self) -> None:
        await self._async_refresh()


class Entity:
    """Base entity with the attributes the integration sets."""

    _attr_has_entity_name = False
    _attr_unique_id: str | None = None
    enabled = True

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    def __init__(self) -> None:
        self.hass = _HASS
        self.entity_id: str | None = None
        self.state: typing.Any = None

    @property
    def should_poll(self) -> bool:
        return True

    def async_write_ha_state(self) -> None:
        """Sync in HA, so a bare call from a platform is correct."""
        self.state = getattr(self, "native_value", None)

    async def async_added_to_hass(self) -> None:
        return None


class CoordinatorEntity(typing.Generic[_CoordinatorT], Entity):
    """Entity kept in sync by a coordinator."""

    def __init__(self, coordinator, context=None) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.coordinator_context = context

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        self.coordinator.async_add_listener(self._handle_coordinator_update)

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()


class DeviceInfo(dict):
    """HA's DeviceInfo is a TypedDict, a plain dict is close enough."""


# ------------------------------------------------------- entity_registry
class RegistryEntry:
    """The sliver of a registry entry the integration reads back."""

    def __init__(self, entity_id: str, domain: str, platform: str,
                 unique_id: str, config_entry_id: str | None = None) -> None:
        self.entity_id = entity_id
        self.domain = domain
        self.platform = platform
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


class EntityRegistry:
    """Stand-in for the entity registry store.

    Only the two calls the integration makes are real: look an entry up by
    its platform and unique id, and remove one. Entries added by a platform
    are not tracked here, the fake never registers entities.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], str] = {}
        self.entries: dict[str, RegistryEntry] = {}
        self.removed: list[str] = []

    def add(self, domain: str, platform: str, unique_id: str,
            entity_id: str, config_entry_id: str | None = None) -> None:
        """Seed an entry the way an earlier release would have left it."""
        self._by_key[(domain, platform, unique_id)] = entity_id
        self.entries[entity_id] = RegistryEntry(
            entity_id, domain, platform, unique_id, config_entry_id
        )

    def async_entries_for_config_entry(
        self, config_entry_id: str
    ) -> list[RegistryEntry]:
        return [
            entry
            for entry in self.entries.values()
            if entry.config_entry_id == config_entry_id
        ]

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        return self._by_key.get((domain, platform, unique_id))

    def async_remove(self, entity_id: str) -> None:
        entry = self.entries.pop(entity_id, None)
        if entry is None:
            # The real registry accepts the removal of a gone entity.
            return
        self._by_key.pop(
            (entry.domain, entry.platform, entry.unique_id), None
        )
        self.removed.append(entity_id)


def async_get_entity_registry(hass: _Harness) -> EntityRegistry:
    """What homeassistant.helpers.entity_registry.async_get returns."""
    return hass.entity_registry


def async_entries_for_config_entry(
    registry: EntityRegistry, arg: str
) -> list[RegistryEntry]:
    """Stand in for the helper of the same name."""
    return registry.async_entries_for_config_entry(arg)


# ---------------------------------------------------------------- sensor
@dataclasses.dataclass(frozen=True, kw_only=True)
class EntityDescription:
    key: str
    translation_key: str | None = None
    entity_category: typing.Any = None
    entity_registry_enabled_default: bool = True
    name: typing.Any = None
    icon: str | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class SensorEntityDescription(EntityDescription):
    device_class: typing.Any = None
    state_class: typing.Any = None
    native_unit_of_measurement: str | None = None
    suggested_unit_of_measurement: str | None = None
    suggested_display_precision: int | None = None
    options: list[str] | None = None
    unit_of_measurement: str | None = None


class SensorDeviceClass:
    VOLTAGE = "voltage"
    CURRENT = "current"
    POWER = "power"
    BATTERY = "battery"
    DURATION = "duration"
    ENERGY = "energy"
    ENUM = "enum"


class SensorStateClass:
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class SensorEntity(Entity):
    """Sensor entity stand-in."""

    _attr_native_unit_of_measurement: str | None = None

    @property
    def native_value(self):
        return None


def _collect(entry: ConfigEntry):
    """AddEntitiesCallback that stores entities on the entry."""

    def add_entities(entities, update_before_add=False) -> None:
        created = getattr(_HASS, "created", [])
        created.extend(entities)
        _HASS.created = created

    return add_entities


# ---------------------------------------------------------------- selectors
class NumberSelectorConfig(dict):
    pass


class NumberSelectorMode:
    BOX = "box"
    SLIDER = "slider"


class _Selector:
    """HA selectors act as validators, so return a validating callable."""

    def __init__(self, config=None) -> None:
        self.config = config

    def __call__(self, value):
        return value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.config})"


class NumberSelector(_Selector):
    pass


class TextSelector(_Selector):
    pass


def install(hass: _Harness, modules: dict) -> _Harness:
    """Hand the fake harness to the module globals and return it."""
    global _HASS
    _HASS = hass
    hass.config_entries = _ConfigEntries(modules)
    return hass


def collect_entities() -> list:
    """Entities handed to async_add_entities during the run."""
    return list(getattr(_HASS, "created", []))


def _unused() -> collections.abc.Iterable:
    return ()
