# Local Development & Troubleshooting Guide

## 1. Set Up a Local Home Assistant Dev Environment

### Option A: Home Assistant Container (quickest)

```bash
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  -v /home/raphael/ha-config:/config \
  -v /home/raphael/Coded/maxspect-homeassistant/custom_components:/config/custom_components \
  --network=host \
  ghcr.io/home-assistant/home-assistant:stable
```

This mounts your working code directly into HA's config, so edits are reflected on restart.

### Option B: HA Core in a Python venv (best for debugging)

```bash
python3 -m venv ha-venv
source ha-venv/bin/activate
pip install homeassistant

mkdir -p ha-config/custom_components
ln -s /home/raphael/Coded/maxspect-homeassistant/custom_components/maxspect \
      ha-config/custom_components/maxspect

hass -c ha-config
```

HA will start on `http://localhost:8123`.

---

## 2. Enable Debug Logging

Add to `ha-config/configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.maxspect: debug
```

Logs appear in the terminal, in **Settings → System → Logs**, and in `ha-config/home-assistant.log`.

---

## 3. Project Structure

```
custom_components/maxspect/
├── __init__.py           # Integration setup / teardown (branches on protocol)
├── api.py                # Gizwits LAN client (state push receiver)
├── cloud.py              # Gizwits Cloud REST API client
├── config_flow.py        # UI-based configuration flow
├── const.py              # Constants, product key → device type mapping
├── coordinator.py        # DataUpdateCoordinator for Gizwits devices
├── entity.py             # Base entities (MaxspectEntity, ICV6Entity)
├── icv6_api.py           # ICV6 binary protocol + async client
├── icv6_coordinator.py   # DataUpdateCoordinator for ICV6 hub
├── sensor.py             # Sensor platform (Gizwits + ICV6)
├── switch.py             # Switch platform (Gizwits + ICV6)
├── manifest.json         # Integration metadata
├── strings.json          # UI strings (source)
└── translations/
    └── en.json           # English translations
```

---

## 4. ICV6 Protocol Overview

The ICV6 controller communicates over **TCP port 80** using a proprietary binary protocol.

### Packet format (new protocol `DD EE FF`)

```
[DD EE FF] [len 2B BE] [FF] [device_id 11B] [module] [cmd] [sub] [payload…] [checksum]
```

- Sub-command byte is at offset 19; payload starts at offset 20.
- Response packets echo the sub-command back (cmd gets `+0x50`).
- `_find_new_packet(resp, expected_sub)` scans a raw TCP buffer and returns the first packet whose sub-command matches.

### Startup behaviour

ICV6 setup is deliberately non-blocking:

1. A quick TCP reachability check is performed (`async_validate_connection`).
2. The integration loads and platforms register immediately with empty coordinator data.
3. Device discovery runs in the background (first coordinator poll).  
   The ICV6 serial bus requires up to 8 warm-up attempts (~35 s on a cold bus).
4. Entities appear dynamically once discovery completes.

### Auto Schedule interpolation

When a device is in **Auto Schedule mode**, `compute_current_levels()` in `icv6_api.py` linearly interpolates between the two bracketing schedule points using wall-clock time. Channel sensors report this live interpolated value; the stored manual setpoint is available as the `manual` extra state attribute.

### Discovery script

`icv6_devices.py` at the repo root is a standalone CLI tool for testing against real hardware:

```bash
python3 icv6_devices.py --ip 192.168.50.247          # discover all devices
python3 icv6_devices.py --ip 192.168.50.247 --device R5S2A001602  # specific device
```

---

## 5. Key Troubleshooting Points

| Area | What to check |
|---|---|
| **ICV6 not found** | Check IP, ensure hub is on the same network. Entities appear after the first poll (~35 s). |
| **ICV6 channel values wrong** | Check mode: Manual → stored setpoint; Auto → interpolated. Look at `extra_state_attributes` for the schedule. |
| **ICV6 warning: no data returned** | Device may be off or unreachable. Check power and cable connections. |
| **Gizwits login/auth** | `config_flow.py` calls the Gizwits API. Check logs for `Login response status`. |
| **Gizwits device models** | `__init__.py` loads JSON model files from `models/`. If your pump's `product_key` doesn't match any model file, entities won't appear. |
| **Gizwits LAN polling** | Integration polls devices locally on **TCP port 12416**. HA host must be on the same network/VLAN. |
| **Cloud control** | Control commands go through Gizwits cloud API. Token expiry or regional mismatch (EU/US/CN) will cause failures. |

---

## 6. Running Tests

```bash
source ha-venv/bin/activate
python3 -m pytest tests/ -v
```

Tests cover the ICV6 protocol helpers, coordinator logic, and all entity types. No real hardware required — all ICV6 I/O is mocked via `unittest.mock`.

---

## 7. Live Editing & Reloading

After code changes:

1. **Quick reload**: HA UI → Settings → Integrations → Maxspect → ⋮ → **Reload**
2. **Full restart**: Stop and re-run `hass -c ha-config` (needed for `__init__.py` or `manifest.json` changes)

---

## 8. Interactive Debugging (Option B only)

```bash
pip install debugpy
python -m debugpy --listen 5678 --wait-for-client -m homeassistant -c ha-config
```

Then attach VS Code:

```json
{
  "name": "Attach to HA",
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "localhost", "port": 5678 }
}
```
