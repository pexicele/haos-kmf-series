"""Constants for the KM-F Series integration."""

from __future__ import annotations

DOMAIN = "kmf_series"
PLATFORMS: list[str] = ["sensor"]

CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"
UPDATE_INTERVAL = "update_interval"

DEFAULT_NAME = "KM-F Series"
DEFAULT_PORT = 8899
DEFAULT_UPDATE_INTERVAL = 30
MIN_UPDATE_INTERVAL = 5
MAX_UPDATE_INTERVAL = 3600

DEVICE_MANUFACTURER = "Junctek"
DEVICE_MODEL = "KM-F Series"

UNIT_AMPERE_HOUR = "Ah"
