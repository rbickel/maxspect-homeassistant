# Maxspect for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Local + cloud control of Maxspect aquarium devices in Home Assistant.

## How It Works

Maxspect devices run on the **Gizwits IoT platform**. This integration uses a hybrid approach:

| Capability | Gyre XF330CE | Other devices |
|------------|-------------|---------------|
| State monitoring | LAN push (fast, local) | Cloud polling |
| Commands (on/off, mode) | Cloud API | Cloud API |

**Cloud credentials are required** for all devices — the Gizwits cloud is used for device discovery and sending commands. The Maxspect firmware ignores command writes over LAN for at least the Gyre XF330CE; it is unknown whether other devices accept LAN commands.

## Features

- **Cloud + LAN hybrid** — state updates via LAN push where supported; commands via the Gizwits cloud API
- **Auto-discovery** — provide your Gizwits account credentials and device IP; the integration handles the rest
- **Pump control** — on/off and mode for Maxspect Gyre pumps
- **Light control** — on/off and channel brightness for Maxspect LED fixtures

## Supported Devices

| Status | Device | Type |
|--------|--------|------|
| ✅ Confirmed | Gyre XF330CE | Pump |
| 🔄 Testing | LED L165 (wifi灯) | Light |
| ❓ Unknown | LED MJ-L265 / L290 | Light |
| ❓ Unknown | LED E8 | Light |
| ❓ Unknown | Aquarium 20 (20缸) | Combo |
| ❓ Unknown | Aquarium System (套缸) | Combo |

**Legend:** ✅ tested and confirmed working — 🔄 testing in progress — ❓ implemented but untested

If you own a device marked ❓ or 🔄, please try the integration and report your experience in the [issues](../../issues).

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
3. Enter the **device IP address** (and optionally port)
4. Enter your **Gizwits / Maxspect app credentials** (username, password, region)

## Development

### Project Structure

```
custom_components/maxspect/
├── __init__.py          # Integration setup / teardown
├── api.py               # Gizwits LAN client (state push receiver)
├── cloud.py             # Gizwits Cloud REST API client (commands)
├── config_flow.py       # UI-based configuration flow
├── const.py             # Constants, product key → device type mapping
├── coordinator.py       # DataUpdateCoordinator (LAN + cloud hybrid)
├── entity.py            # Base entity with device info
├── sensor.py            # Sensor platform
├── switch.py            # Switch platform (power)
├── manifest.json        # Integration metadata
├── strings.json         # UI strings (source)
└── translations/
    └── en.json          # English translations
```

## Contributing & Adding New Devices

**I only own a Gyre XF330CE.** All other device types are implemented based on the decompiled Maxspect app but are untested. I need help from owners of:

- LED L165, MJ-L265/L290, E8
- Aquarium 20 (20缸) / Aquarium System (套缸)

If you own one of these devices and are willing to test, your report is invaluable — even a "it works" or "entities don't appear" comment helps.

### How to report a new device

1. **Enable debug logging** in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.maxspect: debug
   ```

2. Restart Home Assistant and add the integration. In **Settings → System → Logs**, look for your device's product key:

   ```
   Discovered device did=XXXX product_key=<KEY> (online=True)
   ```

3. Try turning the device on and off, then collect the full log section.

4. **[Open a New Device Support issue](../../issues/new?template=new_device_support.md)** with:
   - Your device model and the product key from the logs
   - The debug log from startup through a control action
   - Which entities appeared and whether they responded correctly

Browse [existing device reports](../../issues?q=label%3Anew-device) to see if someone is already testing your model.

### Submit a PR

Code fixes and improvements for untested device types are very welcome. The product key → device type mapping lives in [`const.py`](custom_components/maxspect/const.py) and the per-device control logic is in [`coordinator.py`](custom_components/maxspect/coordinator.py).

---

MIT
