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

DEVICE_MANUFACTURER = "Pexic"
DEVICE_MODEL = "KM-F Series"

UNIT_AMPERE_HOUR = "Ah"

# The part of the unique id the capacity entities used, up to and including
# version 2.1.0 as a sensor and 2.1.1 to 2.1.3 as a number. Neither is
# created any more; the unique id is only kept so their registry entries can
# be removed on upgrade.
LEGACY_CAPACITY_KEY = "set_capacity"
