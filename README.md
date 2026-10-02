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
| State monitoring | LAN push-only (local, device-driven) | Cloud polling |
| Commands (on/off, feeding, resume) | Optional confirmed LAN control; cloud fallback | Cloud API |

**Cloud credentials are required** for all Gizwits devices.

#### Gyre LAN safety and update timing

The integration does **not send LAN attribute-read queries**, including on
startup or reconnect. A live test reproduced a physical change from XF330CE
to XF350CE during the previous nominal-read sequence. Both automatic
timestamp polling and the one-shot configuration query have therefore been
removed. The exact firmware trigger remains uncertain; see the
[captured protocol evidence](MAXSPECT_PROTOCOL.MD#104-physical-pump-profile-change-during-current-lan-read-sequence-2026-10-02).

LAN monitoring retains the connection handshake, heartbeats, and incoming
device pushes. On/off and mode commands still use the cloud API.

- Startup uses cached cloud status when available; it may be stale.
- The device normally sends full status within **60-120 seconds** of connecting.
- Compact RPM, voltage, and power updates typically arrive every **3-5 minutes**.
- Timestamp and configuration updates follow device pushes, not a fixed
  three-second polling interval.

If you used an older version, **check both pump profiles on the physical
controller or in Syna-G** and restore the correct models manually if necessary.
Installing this change does not restore controller settings. Home Assistant's
cached model sensors are not proof that the physical profiles are unchanged.
After installing the updated integration, **restart Home Assistant before
re-enabling it**, so the old Python client is no longer loaded.

## Features

- **ICV6 hub** — fully local, no cloud account; connected LEDs and pumps discovered automatically
- **Live LED brightness** — in Auto Schedule mode the channel sensor shows the interpolated live value, not the stored manual setpoint
- **Schedule visibility** — per-channel schedule points exposed as extra state attributes
- **Cloud + LAN hybrid** — for Gizwits devices, state updates via LAN push and cloud deltas; Gyre commands can use confirmed LAN control with cloud fallback
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
| ✅ Confirmed | Gyre XF350CE | Pump (LAN telemetry, LAN feeding/resume, cloud on/off tested) |
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

XF330CE and XF350CE controllers share a Gizwits product key. The device
registry therefore shows the family name; per-pump model sensors remain
unknown until model metadata is received.

**Protocol correction:** older versions reversed the read/write action bytes.
`0x11` is a write; `0x12` is a read; `0x13` is a read response. The old polling
requests could change device settings. Check the controller's clock, model
selection and feeding duration after upgrading. Invalid feeding values are
ignored by the display; this does not repair already altered settings.

If the controller reports missing or incorrect pump models, open the
integration's **Configure** options and select **XF330CE** or **XF350CE**
for each pump. **Automatic** uses device metadata. These settings only
identify the hardware in Home Assistant; they do not reprogram the pumps.

Choose the region where your app account is hosted, which may differ from
your physical location. If login reports "user does not exist", check the
region as well as the credentials. An XF350CE account used in Europe was
successfully authenticated using the United States region.

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Maxspect"
3. Select **Gizwits device (Gyre pump, LED lights, Aquarium)**
4. Enter the **device IP address** (and optionally port)
5. Enter your **Gizwits / Syna-G+ app credentials** (username, password, region)

#### Feeding and diagnostics

The **Start feeding pause** button uses the duration stored on the controller.
**Resume pumps** exits feeding early. Enable **Prefer confirmed local control**
in Configure to use LAN commands; Home Assistant waits for a matching device
mode report and falls back to cloud control on failure.

All 47 schema data points are parsed internally. Useful decoded values and
remaining diagnostics are exposed as entities. Reserved `Bak` fields and the
redundant raw Auto, Manual, Backup, Model A/B, Time, Countdown Feed and Upgrade
License entities are not created. Unused Reboot, Factory Settings and
Current A/B raw diagnostics are also omitted. Existing entries are removed on
upgrade.
Their underlying data remains available to the decoded sensors and saved
programs. Unreported diagnostics stay unknown and hidden until reported.
Connection/error sensors remain visible even when unknown, and appear in the
device Sensors section alongside the raw channel power sensors. A user-hidden
entity remains hidden when new reports arrive.

**Feeding time remaining** decodes `Countdown_Feed` as three bytes: hours,
minutes, seconds. It reports seconds with a readable `remaining_hms` attribute,
and returns zero after leaving Feed mode so an old countdown is not displayed.
`Time_Feed` is minutes. Firmware is formatted as in Syna-G (`36` → `3.6`), and
serial-number bytes are ASCII. Optional MAC and app-confirmed firmware fields
populate device information; the firmware fallback is labeled as user-confirmed
and a received controller version takes precedence in the sensor.

Controller modes are Water Flow/manual (0), Programming/schedule (1), Feed (2)
and Off (3). Exit Feed (4) and On (5) are commands. Per-pump movement patterns
are separate fields inside the Manual/Auto program blobs.

### Saved schedules and programmed power

Gyre schedules and manual settings are saved using Home Assistant storage and
restored before connecting. Old RPM, connection/error flags and operating mode
are not restored as live readings. Invalid or missing program responses never
replace the last valid saved program.

The **Saved schedule** sensor exposes all daily entries in its `entries`
attribute. **Pump A/B active pattern** and **Pump A/B programmed power** select
the active entry using the controller clock, or Home Assistant local time until
the controller reports its clock after startup. Percentages are programmed
settings, not instantaneous output or electrical watts. Alternating settings
include the signed secondary percentage in `alternate_power_percent`.

**Refresh schedule and settings** requests data without writing settings. The
integration also requests settings over LAN every minute. The refresh status
and last received timestamp distinguish saved values from a fresh report.
On the tested XF350CE firmware, these reads can receive only an acknowledgement
and live deltas; opening the device page in Syna-G triggers a full program
report. The integration preserves saved values when that report is absent.
Reliable app-independent full-program refresh remains unverified, including
when tested through the documented cloud WebSocket read API.

[Example Gyre dashboard](examples/gyre-dashboard.yaml) includes the schedule,
per-pump patterns and percentages, feeding controls, and on-screen renewal
instructions. Adjust its entity IDs to match your installation.

The original channel power sensors remain enabled as **raw** electrical values.
Their former watt unit was unverified. Maxspect specifies **5–52 W for XF350CE**,
but the app defines `Current_A/B` as firmware-specific and `Bak24` as reserved;
there is no documented conversion from these bytes to watts. Do not infer a
scale merely from rated maximum power. Voltage and RPM decoding is unchanged.
See [Maxspect's specifications](https://www.maxspect.com/en/innovate-series/617-gyre-300-ce).

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
