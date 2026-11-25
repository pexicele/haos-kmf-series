# KM-F Series - Home Assistant integration

Custom integration for the **Junctek KM-F Series** battery monitor (KM105F,
KM140F and friends) with the built-in WiFi module.

![KM-F device](assets/device.webp)

Home Assistant opens one TCP connection to the module and keeps it open. The
module pushes its measurement frame on its own, so the values land in Home
Assistant as soon as the device sends them. Nothing goes out to the internet.

![Home Assistant dashboard](assets/dashboard.png)

## Sensors

| Entity | Unit | Notes |
| --- | --- | --- |
| Voltage | V | 2 decimals |
| Current | A | signed, negative while discharging |
| Power | W | signed, from voltage x current |
| State of charge | % | remaining Ah / set capacity |
| Remaining capacity | Ah | |
| Energy charged | kWh | total increasing, from the `:C` frame |
| Energy discharged | kWh | total increasing, from the `:C` frame |
| Status | - | charging / discharging / idle |
| Time remaining | min | disabled by default |
| Set capacity | Ah | disabled by default |

The two disabled-by-default sensors can be enabled from **Settings -> Devices
and Services -> KM-F Series -> Manage**.

Interface strings ship in English and Romanian. Romanian is picked up when
Home Assistant runs in `ro`; anything else falls back to English.

## Installation

### Marketplace (Home Assistant 2026.11 and newer)

The Marketplace replaced HACS inside Home Assistant. A custom repository
needs three things: it is public on GitHub, it has `hacs.json` in its root,
and the integration lives in `custom_components/kmf_series`. This repository
meets all three.

1. Go to **Settings -> Marketplace**.
2. Top right: **Add from link**, then paste
   `https://github.com/pexicele/haos-kmf-series` and select **Add**. Home
   Assistant asks you to connect a GitHub account the first time; the
   connection is read-only and only reads public data.
3. Open **KM-F Series** in the list and select **Install**. If the content
   type is not detected, choose **Integration** under **Type** and add again.
4. Restart Home Assistant when a repair asks for it.

### HACS (Home Assistant 2026.10 and older)

1. **HACS -> Integrations -> three dots -> Custom repositories -> Add
   repository**, category **Integration**, with the URL above.
2. Close that dialog, search for **KM-F Series**, **Download**, then restart
   Home Assistant.

### Manual

1. Download `kmf_series.zip` from the newest release, or clone the
   repository.
2. Copy `custom_components/kmf_series` into
   `<config>/custom_components/kmf_series`. Keep the folder name, it is the
   integration domain.
3. Restart Home Assistant.

### Releases and updates

A release is not required, without one the Marketplace tracks the default
branch and shows the first seven characters of the last commit as the
version. If you do publish one, tag it `v` plus the `version` in
`manifest.json` (for example `v2.0.0`). `.github/workflows/release.yml`
checks that the tag and the manifest agree, runs the tests, and attaches
`kmf_series.zip`, which is only a convenience archive for an offline host.

## Adding the device

**Settings -> Devices & Services -> Add Integration -> KM-F Series**:

- **Host or IP address**: the address of the WiFi module.
- **TCP port**: `8899`, the default on every KM-F.

The setup form reads one frame from the device before it saves anything, so a
wrong address never becomes a broken entry. If it reports that it cannot read
data, check these in order:

1. The module serves **one client at a time**. Close the vendor app and any
   other reader (Node-RED, ESPHome, the add-on that came with the shunt)
   before adding the integration.
2. Home Assistant and the module must be reachable from each other; the
   module does not answer across a routed VLAN unless the firewall allows it.
3. `nc -vz <ip> 8899` from the Home Assistant host should succeed.

Once it is added, the integration reconnects on its own when the module drops
the link, and the entities go unavailable in the meantime.

## Options

**Settings -> Devices & Services -> KM-F Series -> Configure**.

- **Update interval**: seconds between requests for the cumulative energy
  counters (the `:C` frame). The live measurements do not depend on it, they
  arrive pushed. Range 5 to 3600, default 30.

## The protocol

Plain ASCII over TCP on port `8899`, one line per frame, LF terminated.

```
:A=4810,567,0,421,3986,100,...
```

| Field | Meaning | Scale |
| --- | --- | --- |
| 0 | Voltage | /100 -> V |
| 1 | Current, absolute value | /1000 -> A |
| 2 | Direction, 1 charging, 0 discharging | - |
| 3 | Minutes remaining | minutes |
| 4 | Remaining capacity | /1000 -> Ah |
| 5 | Set capacity | /10 -> Ah |
| 6+ | Firmware specific, ignored | - |

```
:C=12,41,...
```

| Field | Meaning | Scale |
| --- | --- | --- |
| 0 | Energy charged | /1000 -> kWh |
| 1 | Energy discharged | /1000 -> kWh |

Sending `:A\n` asks for one measurement frame, `:C\n` for the counters. The
integration uses `:A\n` as a keep alive when the module goes quiet and `:C\n`
on the configured interval.

## Development

```
python3 tests/test_parser.py       # frame parsing
python3 tests/test_client.py       # client against a fake module
python3 tests/test_integration.py  # full lifecycle against a fake HA
```

No third-party test dependencies beyond `voluptuous`, and the tests run
against a plain CPython with a stand-in for the Home Assistant API.

## Credits

The frame layout was worked out from the public KM-F reports and from
`Barlows/ha-addon-km140f` (MIT).

## License

MIT
