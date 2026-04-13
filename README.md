# Maxspect for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant integration for Maxspect aquarium devices, with fully local ICV6 support and Gizwits cloud + LAN hybrid control.

## How It Works

This integration supports two different Maxspect device families:

### ICV6 controller (fully local)

The Maxspect ICV6 is an aquarium hub that controls LED ramps and pumps over a proprietary serial bus.  
All communication happens **locally over TCP port 80** — no cloud account or app is needed.

| Capability | How it works |
|---|---|
| Device discovery | TCP binary protocol, auto-discovered from the hub |
| State polling | Every 30 s (local, no cloud) |
| LED brightness | Current live value, interpolated from the schedule if in Auto mode |
| On/Off control | Local TCP command |

### Gizwits devices (cloud + LAN hybrid)

Maxspect Gyre pumps and LED fixtures use the **Gizwits IoT platform**.

| Capability | Gyre XF330CE | Other devices |
|---|---|---|
| State monitoring | LAN push (fast, local) | Cloud polling |
| Commands (on/off, mode) | Cloud API | Cloud API |

**Cloud credentials are required** for all Gizwits devices.

## Features

- **ICV6 hub** — fully local, no cloud account; connected LEDs and pumps discovered automatically
- **Live LED brightness** — in Auto Schedule mode the channel sensor shows the interpolated live value, not the stored manual setpoint
- **Schedule visibility** — per-channel schedule points exposed as extra state attributes
- **Cloud + LAN hybrid** — for Gizwits devices, state updates via LAN push where supported; commands via the Gizwits cloud API
- **Pump control** — on/off for Maxspect Gyre pumps and ICV6-connected pumps
- **Light control** — on/off and per-channel brightness for Maxspect LED fixtures

## Supported Devices

### ICV6-connected devices (local only)

| Status | Device | Type |
|---|---|---|
| ✅ Confirmed | RSX R5 LED | LED (4 channels) |
| ❓ Unknown | RSX R6 LED | LED (6 channels) |
| ❓ Unknown | Ethereal E5 LED | LED (5 channels) |
| ❓ Unknown | Floodlight LED | LED (4 channels) |
| ❓ Unknown | Turbine Pump T1 | Pump |
| ❓ Unknown | Gyre 2 / Gyre 3 Pump | Pump |
| ❓ Unknown | EggPoints A1 | Pump |

### Gizwits devices (cloud required)

| Status | Device | Type |
|---|---|---|
| ✅ Confirmed | Gyre XF330CE | Pump |
| 🔄 Testing | LED L165 (wifi灯) | Light |
| ❓ Unknown | LED MJ-L265 / L290 | Light |
| ❓ Unknown | LED E8 | Light |
| ❓ Unknown | Aquarium 20 (20缸) | Combo |
| ❓ Unknown | Aquarium System (套缸) | Combo |

**Legend:** ✅ tested and confirmed working — 🔄 testing in progress — ❓ implemented but untested

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

### ICV6 controller

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Maxspect"
3. Select **ICV6 Controller**
4. Enter the **IP address** of the ICV6 hub

Connected LEDs and pumps are discovered automatically after HA starts. Discovery can take up to ~35 seconds on a cold bus — entities will appear once the first poll completes.

### Gizwits devices

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Maxspect"
3. Select **Gizwits device (Gyre pump, LED lights, Aquarium)**
4. Enter the **device IP address** (and optionally port)
5. Enter your **Gizwits / Syna-G+ app credentials** (username, password, region)

## Contributing & Adding New Devices

**ICV6 device owners:** if you own an ICV6-connected device marked ❓, please try the integration and open an issue with your experience.

**Gizwits device owners:** I only own a Gyre XF330CE. To test other Gizwits devices:

1. **Enable debug logging** in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.maxspect: debug
   ```

2. Restart HA and add the integration. Look for your device's product key in the logs:

   ```
   Discovered device did=XXXX product_key=<KEY> (online=True)
   ```

3. **[Open a New Device Support issue](../../issues/new?template=new_device_support.md)** with:
   - Your device model and product key
   - The debug log from startup through a control action
   - Which entities appeared and whether they responded correctly

### Submit a PR

Code fixes and improvements are welcome. Key files:

- ICV6 protocol: [`icv6_api.py`](custom_components/maxspect/icv6_api.py), [`icv6_coordinator.py`](custom_components/maxspect/icv6_coordinator.py)
- Gizwits devices: [`const.py`](custom_components/maxspect/const.py), [`coordinator.py`](custom_components/maxspect/coordinator.py)

---

MIT
