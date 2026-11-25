"""Custom Home Assistant integration for the Junctek KM-F Series."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import KmfcConfigEntry, KmfcCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NUMBER]


async def async_setup_entry(
    hass: HomeAssistant, entry: KmfcConfigEntry
) -> bool:
    """Set up a KM-F device from a config entry."""
    coordinator = KmfcCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


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
