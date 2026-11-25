"""Constants for the KM-F Series integration."""

from __future__ import annotations

DOMAIN = "kmf_series"
PLATFORMS: list[str] = ["sensor", "number"]

CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"
UPDATE_INTERVAL = "update_interval"

DEFAULT_NAME = "KM-F Series"
DEFAULT_PORT = 8899
DEFAULT_UPDATE_INTERVAL = 30
MIN_UPDATE_INTERVAL = 5
MAX_UPDATE_INTERVAL = 3600

DEVICE_MANUFACTURER = "Pexic"
DEVICE_MODEL = "KM-F Series"

UNIT_AMPERE_HOUR = "Ah"

# Bounds of the capacity the user may write, in whole ampere hours.
MIN_CAPACITY = 1
MAX_CAPACITY = 9999
