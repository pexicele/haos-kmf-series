"""End to end check of the integration against a fake Home Assistant.

Drives the real integration modules -- __init__, coordinator, sensor and
config_flow -- against a simulated KM-F module on localhost, with the fake
HA from fakeha.py standing in for the platform. The registry cleanup that
runs on setup is exercised too, against entries an older release left
behind:

    python3 tests/test_integration.py

It checks the parts a unit test cannot reach: entry setup, entity creation,
value mapping, the availability flag and the unload path.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import fakeha
import _loader

logging.basicConfig(level=logging.CRITICAL)

A_FRAME = b":A=4810,567,0,421,3986,100,\n"
C_FRAME = b":C=12,41,\n"

# Capacity the fake module stores, tenths of an Ah, in the :A frame. The
# integration only reads it, the setting itself is changed on the meter.
STORED_CAPACITY = 11000

# Every line the integration sent during the run. The release that removed
# the capacity setting must send no write frame at all.
SEEN_REQUESTS: list[bytes] = []


def _install_fake_ha() -> None:
    """Register fakeha under the homeassistant package names."""
    pkg = types.ModuleType("homeassistant")
    pkg.__path__ = []  # type: ignore[attr-defined]

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    core = module("homeassistant.core")
    core.HomeAssistant = fakeha.HomeAssistant
    core.callback = fakeha.callback

    const = module("homeassistant.const")
    const.PERCENTAGE = fakeha.PERCENTAGE
    const.Platform = fakeha.Platform
    const.EntityCategory = fakeha.EntityCategory
    for name in (
        "UnitOfTime",
        "UnitOfElectricCurrent",
        "UnitOfElectricPotential",
        "UnitOfEnergy",
        "UnitOfPower",
    ):
        setattr(const, name, getattr(fakeha, name))

    exceptions = module("homeassistant.exceptions")
    for name in (
        "HomeAssistantError",
        "ConfigEntryError",
        "ConfigEntryNotReady",
        "ConfigEntryAuthFailed",
    ):
        setattr(exceptions, name, getattr(fakeha, name))

    config_entries = module("homeassistant.config_entries")
    config_entries.ConfigEntry = fakeha.ConfigEntry
    config_entries.ConfigFlow = fakeha.ConfigFlow
    config_entries.ConfigFlowResult = (  # type: ignore[attr-defined]
        fakeha.ConfigFlowResult
    )
    config_entries.OptionsFlow = fakeha.OptionsFlow

    helpers = module("homeassistant.helpers")
    update_coordinator = module("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = (  # type: ignore[attr-defined]
        fakeha.DataUpdateCoordinator
    )
    update_coordinator.UpdateFailed = fakeha.UpdateFailed
    update_coordinator.CoordinatorEntity = (  # type: ignore[attr-defined]
        fakeha.CoordinatorEntity
    )

    device_registry = module("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = fakeha.DeviceInfo

    entity_platform = module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object  # type: ignore[attr-defined]

    entity_registry = module("homeassistant.helpers.entity_registry")
    # The calls the integration reaches the registry through.
    entity_registry.async_get = (  # type: ignore[attr-defined]
        fakeha.async_get_entity_registry
    )
    entity_registry.async_entries_for_config_entry = (  # type: ignore
        fakeha.async_entries_for_config_entry
    )

    selector = module("homeassistant.helpers.selector")
    for name in (
        "NumberSelector",
        "NumberSelectorConfig",
        "NumberSelectorMode",
        "TextSelector",
    ):
        setattr(selector, name, getattr(fakeha, name))

    components = module("homeassistant.components")
    sensor = module("homeassistant.components.sensor")
    sensor.SensorDeviceClass = fakeha.SensorDeviceClass
    sensor.SensorEntity = fakeha.SensorEntity
    sensor.SensorEntityDescription = (  # type: ignore[attr-defined]
        fakeha.SensorEntityDescription
    )
    sensor.SensorStateClass = fakeha.SensorStateClass

    for name, mod in (
        ("core", core),
        ("const", const),
        ("exceptions", exceptions),
        ("config_entries", config_entries),
        ("helpers", helpers),
        ("components", components),
    ):
        setattr(pkg, name, mod)

    sys.modules["homeassistant"] = pkg

    helpers.update_coordinator = update_coordinator
    helpers.device_registry = device_registry
    helpers.entity_platform = entity_platform
    helpers.entity_registry = entity_registry
    helpers.selector = selector
    components.sensor = sensor


async def fake_module(reader, writer) -> None:
    """Push frames and answer the :A and :C requests, nothing else."""
    global STORED_CAPACITY
    writer.write(b":A=4810,567,0,421,3986,%d,\n" % STORED_CAPACITY)
    await writer.drain()
    try:
        while True:
            request = await reader.read(128)
            if not request:
                break
            SEEN_REQUESTS.append(request)
            if b":C" in request:
                writer.write(C_FRAME)
                await writer.drain()
            if b":A" in request:
                writer.write(b":A=4810,567,0,421,3986,%d,\n" % STORED_CAPACITY)
                await writer.drain()
            # No ":W" branch here on purpose. The integration must never
            # send one, and SEEN_REQUESTS is what proves it.
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


def entity_map() -> dict:
    """Entities created so far, keyed by their unique id."""
    return {e.unique_id: e for e in fakeha.collect_entities()}


async def main() -> int:
    """Run the full lifecycle once."""
    _install_fake_ha()

    integration = _loader.load("__init__")
    sensor_platform = _loader.load("sensor")
    config_flow_module = _loader.load("config_flow")

    hass = fakeha.HomeAssistant()
    fakeha.install(hass, {"sensor": sensor_platform})

    # What the earlier releases left in the registry: the capacity first as
    # a disabled sensor (2.1.0), then as a configuration number
    # (2.1.1 to 2.1.3), and a number under the address the device had before
    # it was moved. Setup has to take all three out again.
    registry = hass.entity_registry
    registry.add("sensor", "kmf_series", "127.0.0.1_set_capacity",
                 "sensor.kmf_set_capacity", "test-entry")
    registry.add("number", "kmf_series", "127.0.0.1_set_capacity",
                 "number.kmf_set_capacity", "test-entry")
    registry.add("number", "kmf_series", "192.168.1.50_set_capacity",
                 "number.kmf_set_capacity_old", "test-entry")
    # A sensor of this entry that has to survive the sweep.
    registry.add("sensor", "kmf_series", "127.0.0.1_voltage",
                 "sensor.kmf_voltage", "test-entry")

    server = await asyncio.start_server(fake_module, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    failures = 0

    def report(label: str, ok: bool, detail: object = "") -> None:
        nonlocal failures
        if ok:
            print(f"ok   {label}: {detail}")
        else:
            failures += 1
            print(f"FAIL {label}: {detail}")

    async with server:
        # --- config flow validates a live device -----------------------
        flow = config_flow_module.KmfcConfigFlow()
        result = await flow.async_step_user(
            {"name": "Balcony", "host": "127.0.0.1", "port": port}
        )
        report(
            "config flow creates an entry",
            result["type"] == "create_entry"
            and result["data"]["host"] == "127.0.0.1",
            result.get("data"),
        )

        bad = config_flow_module.KmfcConfigFlow()
        result = await bad.async_step_user(
            {"name": "X", "host": "127.0.0.1", "port": 1}
        )
        report(
            "config flow rejects an unreachable device",
            result["type"] == "form"
            and result["errors"].get("cannot_connect") == "cannot_connect",
            result.get("errors"),
        )

        bad_host = config_flow_module.KmfcConfigFlow()
        result = await bad_host.async_step_user(
            {"name": "X", "host": "not a host!", "port": 8899}
        )
        report(
            "config flow rejects a bad hostname",
            result["type"] == "form"
            and result["errors"].get("host") == "invalid_host",
            result.get("errors"),
        )

        options_flow = config_flow_module.KmfcOptionsFlow(
            fakeha.ConfigEntry({"host": "x"}, {"update_interval": 45})
        )
        result = await options_flow.async_step_init()
        report(
            "options flow shows the interval",
            result["type"] == "form" and result["step_id"] == "init",
            result["type"],
        )

        # --- entry setup creates live entities -------------------------
        entry = fakeha.ConfigEntry(
            {"name": "Balcony", "host": "127.0.0.1", "port": port},
            {"update_interval": 1},
        )
        setup_ok = await integration.async_setup_entry(hass, entry)
        report("setup entry", setup_ok is True, setup_ok)

        coordinator = hass.data["kmf_series"][entry.entry_id]
        for _ in range(80):
            await asyncio.sleep(0.05)
            if len(fakeha.collect_entities()) >= 10 and (
                coordinator.data or {}).get("energy_charged") is not None:
                break
        entities = entity_map()
        report(
            "10 sensors created, no number",
            len(entities) == 10 and not any(
                type(e).__name__ == "KmfcCapacityNumber"
                for e in fakeha.collect_entities()
            ),
            f"{len(entities)} created",
        )
        report(
            "no capacity entity in the registry",
            registry.async_get_entity_id(
                "number", "kmf_series", "127.0.0.1_set_capacity"
            )
            is None
            and registry.async_get_entity_id(
                "sensor", "kmf_series", "127.0.0.1_set_capacity"
            )
            is None,
            registry.removed,
        )
        report(
            "left over registry entries removed",
            sorted(registry.removed)
            == [
                "number.kmf_set_capacity",
                "number.kmf_set_capacity_old",
                "sensor.kmf_set_capacity",
            ],
            registry.removed,
        )
        report(
            "the live sensors are left alone",
            registry.async_get_entity_id(
                "sensor", "kmf_series", "127.0.0.1_voltage"
            )
            == "sensor.kmf_voltage",
            sorted(registry.entries),
        )

        for entity in fakeha.collect_entities():
            await entity.async_added_to_hass()
            entity.async_write_ha_state()

        expected = {
            "127.0.0.1_voltage": 48.1,
            "127.0.0.1_current": -0.567,
            "127.0.0.1_power": -27.3,
            # 3.986 Ah left of the 1100 Ah the fake module stores.
            "127.0.0.1_soc": 0.4,
            "127.0.0.1_status": "discharging",
            "127.0.0.1_energy_charged": 0.012,
            "127.0.0.1_energy_discharged": 0.041,
        }
        for unique_id, want in expected.items():
            entity = entities.get(unique_id)
            got = getattr(entity, "state", None) if entity else None
            report(f"{unique_id}", entity is not None and got == want, got)

        entity = entities.get("127.0.0.1_voltage")
        report(
            "device info bound",
            bool(entity and entity._attr_device_info.get("identifiers")),
            dict(entity._attr_device_info) if entity else None,
        )
        report(
            "available while streaming",
            bool(entity and entity.available),
            entity.available if entity else None,
        )

        # --- nothing is written to the meter ---------------------------
        report(
            "no write frame leaves the integration",
            not any(b":W" in request for request in SEEN_REQUESTS),
            f"{len(SEEN_REQUESTS)} requests sent, none a write",
        )
        report(
            "reported capacity still reads from the stream",
            entities["127.0.0.1_reported_capacity"].state == 1100.0,
            entities["127.0.0.1_reported_capacity"].state,
        )

        # --- losing the device flips availability ----------------------
        await coordinator.shutdown()
        await asyncio.sleep(0.1)
        report(
            "unavailable after the link drops",
            not entities["127.0.0.1_voltage"].available,
            entities["127.0.0.1_voltage"].available,
        )

        # --- unload closes everything ---------------------------------
        unloaded = await integration.async_unload_entry(hass, entry)
        report("unload entry", unloaded is True, unloaded)
        report(
            "entry data cleaned up",
            entry.entry_id not in hass.data["kmf_series"],
            list(hass.data["kmf_series"]),
        )

    server.close()
    await server.wait_closed()
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} ({failures} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
