"""Parsing of the Junctek KM-F ASCII stream.

This module is intentionally free of Home Assistant imports so it can be
unit tested on its own (see tests/test_parser.py).

Wire format, one frame per line, terminated by LF:

    :A=5138,4930,0,1822,950453,12500,...,
    :C=0,41,0,0,0,0,0,0,4509,

:A= fields (index: meaning, raw scale):
    0 voltage        1/100  V
    1 current        1/1000 A, unsigned, sign comes from field 2
    2 direction      1 = charging, 0 = discharging
    3 time left      minutes
    4 remaining Ah   1/1000 Ah
    5 capacity set   1/10   Ah
    6+ extra fields, seen on some firmware, ignored

:C= fields:
    0 energy charged    1/1000 kWh, cumulative
    1 energy discharged 1/1000 kWh, cumulative

The scaling above was confirmed against the readings shown on the display
of a KM-F unit and against the field tests published for the KM105F and
KM140F models.
"""

from __future__ import annotations

from typing import Any

A_PREFIX = ":A="
C_PREFIX = ":C="

VOLTAGE_SCALE = 100.0
CURRENT_SCALE = 1000.0
AMPHOUR_SCALE = 1000.0
CAPACITY_SCALE = 10.0
ENERGY_SCALE = 1000.0

STATUS_CHARGING = "charging"
STATUS_DISCHARGING = "discharging"
STATUS_IDLE = "idle"

MIN_A_FIELDS = 6
MIN_C_FIELDS = 2


def _split_payload(line: str, prefix: str) -> list[str]:
    """Return the comma separated fields of a frame, ignoring padding."""
    payload = line[len(prefix):]
    return payload.rstrip(",").split(",")


def parse_a_line(line: str) -> dict[str, Any] | None:
    """Parse one :A= frame into a dict of HA-ready values."""
    fields = _split_payload(line, A_PREFIX)
    if len(fields) < MIN_A_FIELDS:
        return None

    try:
        raw_voltage = int(fields[0])
        raw_current = int(fields[1])
        direction = int(fields[2])
        minutes_left = int(fields[3])
        raw_remaining = int(fields[4])
        raw_capacity = int(fields[5])
    except ValueError:
        return None

    voltage = raw_voltage / VOLTAGE_SCALE
    current = raw_current / CURRENT_SCALE
    charging = direction == 1

    # Multiply the raw integers first so the power does not drift the way
    # a product of two floats would.
    power = (raw_voltage * raw_current) / (VOLTAGE_SCALE * CURRENT_SCALE)

    remaining_ah = raw_remaining / AMPHOUR_SCALE
    capacity_ah = raw_capacity / CAPACITY_SCALE

    soc: float | None = None
    if capacity_ah > 0:
        ratio = remaining_ah / capacity_ah
        soc = round(min(100.0, max(0.0, ratio * 100.0)), 1)

    if current == 0:
        status = STATUS_IDLE
    else:
        status = STATUS_CHARGING if charging else STATUS_DISCHARGING

    return {
        "voltage": round(voltage, 2),
        "current": round(current if charging else -current, 3),
        "power": round(power if charging else -power, 1),
        "time_remaining": minutes_left,
        "remaining_capacity": round(remaining_ah, 3),
        # What the meter has stored as its preset capacity. Read only
        # here: the setting itself lives on the device.
        "reported_capacity": round(capacity_ah, 1),
        "soc": soc,
        "status": status,
        "charging": charging,
    }


def parse_c_line(line: str) -> dict[str, Any] | None:
    """Parse one :C= frame into a dict of HA-ready values."""
    fields = _split_payload(line, C_PREFIX)
    if len(fields) < MIN_C_FIELDS:
        return None

    try:
        raw_charged = int(fields[0])
        raw_discharged = int(fields[1])
    except ValueError:
        return None

    return {
        "energy_charged": round(raw_charged / ENERGY_SCALE, 3),
        "energy_discharged": round(raw_discharged / ENERGY_SCALE, 3),
    }


def parse_line(line: str) -> dict[str, Any] | None:
    """Parse one line of the stream, or None when it is not a frame."""
    line = line.strip()
    if not line:
        return None
    if line.startswith(A_PREFIX):
        return parse_a_line(line)
    if line.startswith(C_PREFIX):
        return parse_c_line(line)
    return None
