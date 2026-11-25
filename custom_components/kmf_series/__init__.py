"""Custom Home Assistant integration for the Junctek KM-F Series."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_HOST,
    DOMAIN,
    LEGACY_CAPACITY_KEY,
)
from .coordinator import KmfcConfigEntry, KmfcCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# The platforms that carried the writable capacity, in the releases that
# had one: 2.1.0 as a sensor, 2.1.1 to 2.1.3 as a number.
CAPACITY_PLATFORMS: tuple[str, ...] = ("sensor", "number")

# Every unique id the capacity entities used ends like this, whatever host
# was written into it.
CAPACITY_SUFFIX = f"_{LEGACY_CAPACITY_KEY}"


async def async_setup_entry(
    hass: HomeAssistant, entry: KmfcConfigEntry
) -> bool:
    """Set up a KM-F device from a config entry."""
    await _async_remove_capacity_entities(hass, entry)

    coordinator = KmfcCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_remove_capacity_entities(
    hass: HomeAssistant, entry: KmfcConfigEntry
) -> None:
    """Forget the capacity entities earlier releases created.

    The setting is gone from the integration, so a registry entry left
    behind belongs to nothing. Home Assistant still lists it on the device
    page, disabled and unusable, and only deletes it when asked. This runs
    before the platforms are set up, and no platform claims those unique ids
    any more, so there is nothing to collide with.

    Two lookups are made, because the unique id carries the host the entry
    had when the entity was created: the id built from the current host, and
    every entry of this config entry whose unique id ends the same way. The
    second one catches an address that was changed since.

    A failure here costs a stale row on the device page. It must not cost
    the readings, so nothing is allowed to escape.
    """
    host = str(entry.data.get(CONF_HOST, ""))
    try:
        registry = er.async_get(hass)
        stale: set[str] = set()
        if host:
            for platform in CAPACITY_PLATFORMS:
                entity_id = registry.async_get_entity_id(
                    platform, DOMAIN, f"{host}{CAPACITY_SUFFIX}"
                )
                if entity_id is not None:
                    stale.add(entity_id)
        for item in er.async_entries_for_config_entry(
            registry, entry.entry_id
        ):
            if (item.unique_id or "").endswith(CAPACITY_SUFFIX):
                stale.add(item.entity_id)
        for entity_id in sorted(stale):
            registry.async_remove(entity_id)
            _LOGGER.info(
                "Removed %s, left over from the capacity entity of "
                "earlier versions",
                entity_id,
            )
    except Exception as err:  # noqa: BLE001 - stale rows are not fatal
        _LOGGER.debug(
            "Could not clean up the old capacity entities of %s: %s",
            host,
            err,
        )


async def async_unload_entry(
    hass: HomeAssistant, entry: KmfcConfigEntry
) -> bool:
    """Unload a config entry and close its socket."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if not unload_ok:
        return False

    coordinator: KmfcCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.shutdown()
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: KmfcConfigEntry
) -> None:
    """Reload the entry when the host, the port or the interval changes."""
    await hass.config_entries.async_reload(entry.entry_id)
