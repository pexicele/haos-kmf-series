"""Checks for the KM-F ASCII parser.

These run without Home Assistant installed:

    python3 tests/test_parser.py
    python3 -m pytest tests/test_parser.py
"""

from __future__ import annotations

import _loader

parser = _loader.load("parser")
parse_a_line = parser.parse_a_line
parse_c_line = parser.parse_c_line
parse_line = parser.parse_line

# Captured over "nc <ip> 8899" from a KM-F WiFi module.
A_FRAME = ":A=4810,567,0,421,3986,100,50,150500,1822,"
C_FRAME = ":C=0,41,0,0,0,0,0,0,4509,"

# Second :A= sample with a different direction bit, 1250 Ah pack.
A_FRAME_CHARGING = ":A=5138,4930,1,1822,950453,12500,"


def check(label: str, got: object, want: object) -> None:
    """Compare and report, keeping the output readable without pytest."""
    status = "ok  " if got == want else "FAIL"
    print(f"{status} {label}: {got!r}")
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def test_a_frame() -> None:
    """Field scaling of a discharging frame."""
    frame = parse_line(A_FRAME)
    assert frame is not None
    check("voltage", frame["voltage"], 48.10)
    check("current signed", frame["current"], -0.567)
    check("power signed", frame["power"], -27.3)
    check("time remaining", frame["time_remaining"], 421)
    check("remaining Ah", frame["remaining_capacity"], 3.986)
    check("set capacity", frame["set_capacity"], 10.0)
    check("soc", frame["soc"], 39.9)
    check("status", frame["status"], "discharging")
    check("charging flag", frame["charging"], False)


def test_a_frame_charging() -> None:
    """Positive direction bit flips current, power and status."""
    frame = parse_a_line(A_FRAME_CHARGING)
    assert frame is not None
    check("voltage", frame["voltage"], 51.38)
    check("current signed", frame["current"], 4.93)
    # 51.38 V x 4.93 A = 253.3 W
    check("power signed", frame["power"], 253.3)
    check("remaining Ah", frame["remaining_capacity"], 950.453)
    check("set capacity", frame["set_capacity"], 1250.0)
    check("soc", frame["soc"], 76.0)
    check("status", frame["status"], "charging")


def test_c_frame() -> None:
    """Cumulative counters come from the first two fields."""
    frame = parse_c_line(C_FRAME)
    assert frame is not None
    check("charged kWh", frame["energy_charged"], 0.0)
    check("discharged kWh", frame["energy_discharged"], 0.041)


def test_rejects() -> None:
    """Short lines, junk and a zero capacity pack are handled."""
    check("short A", parse_line(":A=1,2,3"), None)
    check("junk", parse_line("hello"), None)
    check("empty", parse_line("   "), None)
    check("short C", parse_line(":C=1"), None)
    zero = parse_line(":A=4810,567,0,421,3986,0,")
    assert zero is not None
    check("soc with zero capacity", zero["soc"], None)
    idle = parse_line(":A=4810,0,0,421,3986,100,")
    assert idle is not None
    check("idle status", idle["status"], "idle")
    bad = parse_line(":A=xx,567,0,421,3986,100,")
    check("non numeric", bad, None)


def main() -> int:
    """Run every test function in this module."""
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
            except AssertionError:
                failures += 1
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} ({failures} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
