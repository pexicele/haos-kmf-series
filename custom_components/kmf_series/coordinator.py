"""Update coordinator for one KM-F device.

The KM-F stream is push based, so the usual "poll every N seconds" model does
not fit. This coordinator owns a single reader task for the lifetime of a
config entry. The reader hands every parsed frame to a listener; the listener
only schedules a Home Assistant refresh when the values actually moved, so a
chatty device does not spam the recorder.

When the socket drops, the reader raises, the coordinator marks the device
unavailable and Home Assistant retries on the normal coordinator cadence.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .client import (
    DEFAULT_PORT,
    KmfcClient,
    KmfcConnectionError,
)
from .const import (
    CONF_HOST,
    CONF_PORT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# The config entry carries the coordinator in runtime_data. The string form
# keeps the alias usable before the class below is defined.
KmfcConfigEntry = ConfigEntry["KmfcCoordinator"]


class KmfcCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keep one streaming connection per config entry."""

    config_entry: KmfcConfigEntry

    def __init__(self, hass: HomeAssistant, entry: KmfcConfigEntry) -> None:
        """Build the client and the coordinator for this entry."""
        interval = max(
            1, int(entry.options.get(UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
        )
        self.available = False
        self._latest: dict[str, Any] = {}
        self._reader_task: asyncio.Task | None = None
        self._client = KmfcClient(
            str(entry.data[CONF_HOST]),
            int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
            counters_interval=float(interval),
            on_data=self._handle_frame,
            on_connection_change=self._handle_connection_change,
        )

    @property
    def host(self) -> str:
        """Host of the device, used in unique ids and log messages."""
        return str(self.entry.data[CONF_HOST])

    @property
    def entry(self) -> KmfcConfigEntry:
        """Shortcut to the typed config entry."""
        return self.config_entry

    def _handle_connection_change(self, connected: bool) -> None:
        """Track the link state; the next refresh publishes it."""
        self.available = connected

    def _handle_frame(self, data: dict[str, Any]) -> None:
        """Merge a frame and wake the entities only when something moved."""
        changed = False
        for key, value in data.items():
            if self._latest.get(key) != value:
                self._latest[key] = value
                changed = True
        if changed:
            self.async_set_updated_data(dict(self._latest))

    async def _async_setup(self) -> None:
        """Probe the device once, before the entities are created."""
        try:
            frame = await self._client.probe()
        except KmfcConnectionError as err:
            self.available = False
            raise ConfigEntryNotReady(str(err)) from err
        self._latest.update(frame)
        self.available = True
        _LOGGER.debug("KM-F at %s answered %s", self.host, frame)

    async def _async_update_data(self) -> dict[str, Any]:
        """Keep the reader alive and hand out the newest frame."""
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = self.hass.async_create_task(
                self._run_reader(), f"{DOMAIN}_reader_{self.entry.entry_id}"
            )
        if not self._latest:
            raise UpdateFailed(f"No data from KM-F at {self.host} yet")
        return dict(self._latest)

    async def _run_reader(self) -> None:
        """Stream until the device drops out, then let Home Assistant retry."""
        try:
            await self._client.listen()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, KmfcConnectionError) as err:
            _LOGGER.debug("KM-F stream ended for %s: %s", self.host, err)
        self.available = False
        self.async_update_listeners()

    async def shutdown(self) -> None:
        """Stop the reader when the entry is unloaded or removed."""
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._client.stop()
