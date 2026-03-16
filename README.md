# Maxspect for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Local (LAN) control of Maxspect aquarium devices in Home Assistant.

## Features

- **Local control** — communicates directly with devices on your network, no cloud required
- **Auto-discovery** — enter the device IP and the integration handles the rest
- **Light control** — on/off and brightness for Maxspect LED fixtures

## Supported Devices

> **Note:** The API endpoints in this integration are placeholders. You will need to reverse-engineer or document the actual Maxspect local protocol and update `api.py` accordingly.

Potential targets:

- Maxspect Ethereal (LED light)
- Maxspect Jump (LED light)
- Maxspect RSX / R420R
- Maxspect Gyre series (pumps — would use a `fan` or `switch` platform)
- Maxspect MJ series

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **⋮** → **Custom repositories**
3. Add this repository URL and select **Integration** as the category
4. Search for "Maxspect" and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/maxspect` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Maxspect"
3. Enter the IP address (and optionally port) of your device

## Development

### Project Structure

```
custom_components/maxspect/
├── __init__.py          # Integration setup / teardown
├── api.py               # Local HTTP client for device communication
├── config_flow.py       # UI-based configuration flow
├── const.py             # Constants (domain, defaults)
├── coordinator.py       # DataUpdateCoordinator for polling
├── entity.py            # Base entity with device info
├── light.py             # Light platform entity
├── manifest.json        # Integration metadata
├── strings.json         # UI strings (source)
└── translations/
    └── en.json          # English translations
```

### Implementing the Device Protocol

The main work to make this functional is in [api.py](custom_components/maxspect/api.py). You need to:

1. **Discover the device protocol** — use a packet sniffer (e.g., Wireshark) or the Maxspect app to capture network traffic
2. **Replace placeholder endpoints** — update the HTTP calls (or switch to raw TCP/UDP) based on the real protocol
3. **Update data models** — adjust `MaxspectDeviceInfo` and `MaxspectDeviceState` to match actual device responses
4. **Add platforms** — for pumps, add `fan.py` or `switch.py`; for sensors (temperature, etc.), add `sensor.py`

## License

MIT
