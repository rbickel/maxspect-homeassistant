---
description: Guidelines for working on the Maxspect Gyre Home Assistant integration — architecture, protocol, known pitfalls, testing
applyTo: "custom_components/maxspect/**,tests/**,_agent_workdir/**"
---

## File organisation

- Temporary, working, and diagnostic scripts belong in `_agent_workdir/` (git-ignored).
- Tests belong in `tests/` and must run with `pytest` from the project root.
- Protocol documentation lives in `MAXSPECT_PROTOCOL.md`. Update it whenever you reverse-engineer new behaviour from the device.

---

## Architecture — know before touching anything

The integration uses **two separate channels** that must never be confused:

| Channel | Direction | Client | Purpose |
|---|---|---|---|
| LAN TCP :12416 | Read only | `api.py::MaxspectClient` | Receives real-time device pushes (mode, RPM, voltage, power) |
| Gizwits Cloud HTTPS | Write only | `cloud.py::GizwitsCloudClient` | Sends control commands (mode changes) |

**LAN writes are silently ignored by the device firmware.** `api.py::async_set_mode` sends a LAN write but the device discards it. Control must always go through the cloud API (`coordinator.async_set_mode`).

### Data flow

```
Device (TCP push) → MaxspectClient._process_push()
  → _parse_compact_telemetry() or _parse_state_notify() or mode update
  → MaxspectDeviceState (mutable dataclass, shared object)
  → _update_callback() → coordinator._on_device_push()
  → coordinator.async_set_updated_data() → HA entity update
```

`coordinator.data` and `client.state` **point to the same `MaxspectDeviceState` instance**. Mutating `client.state` also mutates `coordinator.data`. Never assume they are independent snapshots.

---

## Device modes (DP 18)

| Value | Constant | Pumps spinning? | `is_on` |
|---|---|---|---|
| 0 | `MODE_WATER_FLOW` | Yes | `True` |
| 1 | `MODE_PROGRAMMING` | Yes (on schedule) | `True` |
| 2 | `MODE_FEED` | **No** (paused) | `True` |
| 3 | `MODE_OFF` | No | `False` |
| 4 | `MODE_EXIT_FEED` | Transitional | `True` |
| 5 | `MODE_ON` | Yes | `True` |

`is_on = mode != MODE_OFF` — it reflects "device on", not "gyres spinning". **Feed mode (2) is intentionally `is_on=True`**: the device is on, just paused for feeding. The mode sensor (`"Feed"`) disambiguates this in the UI. Do not change this without a deliberate UX decision.

---

## Known flakiness pattern — write cooldown

**Symptom**: switch shows ON while gyres are physically OFF (or vice versa) for a few seconds after a user toggle.

**Root cause**: after `coordinator.async_set_mode(MODE_OFF)`:
1. Cloud API responds (HTTP 200) — command sent.
2. Coordinator applies optimistic update: `state.is_on = False`.
3. Device takes 1–5 s to process the cloud command.
4. During that window the LAN listener keeps receiving old compact-telemetry pushes (mode=5 ON), which mutate the shared `state` object back to `is_on=True` via `_parse_compact_telemetry`.
5. Because `coordinator.data is client.state` (same object), entity reads pick up the flipped value.

**Fix in place** (`coordinator.py`):
- `_pending_mode` tracks the last written mode.
- `_write_lock_until` is set to `time.monotonic() + 8.0` after every successful cloud write.
- `_on_device_push` during cooldown:
  - If LAN mode **matches** pending → device confirmed; lift cooldown early, notify.
  - If LAN mode **differs** → stale push; re-apply `_pending_mode` onto `state` and return (no notification).

**Do not remove or weaken this cooldown** without a clear alternative. 8 seconds was chosen conservatively; real round-trip is typically 1–3 s.

---

## ICV6 exponential back-off for unreachable devices

**Symptom**: ICV6 child device (LED or pump) persistently unreachable — floods the log with warnings on every discovery cycle (every 300s).

**Root cause**:
- Device is powered off, in standby mode, disconnected from the ICV6 hub's serial bus, or otherwise unreachable
- Without back-off, the coordinator polls the device on every discovery cycle regardless of how many failures occurred
- This wastes CPU cycles and floods logs without any benefit

**Fix in place** (`icv6_coordinator.py`):
- `_device_failures` dict tracks consecutive failure count per device
- `_device_last_attempt` dict tracks timestamp of last polling attempt per device
- After `_BACKOFF_THRESHOLD` (3) consecutive failures, exponential back-off is enabled
- Back-off interval: `_REDISCOVER_INTERVAL * min(2^(failures - threshold), _BACKOFF_MAX_MULTIPLIER)`
  - First back-off: 300s * 2^1 = 600s (10 minutes)
  - Second: 300s * 2^2 = 1200s (20 minutes)
  - Third: 300s * 2^3 = 2400s (40 minutes)
  - Capped at: 300s * 8 = 2400s maximum (40 minutes)
- Device is not polled during back-off period unless interval has elapsed
- On successful device response, failure counter is reset and normal polling resumes
- Entities remain available with last known state during back-off (graceful degradation)

**Logging behavior**:
- First 2 failures: log warning with error details
- At 3rd failure (threshold): log warning announcing back-off enabled
- During back-off: no warnings (silent skip)
- On recovery: log info message announcing resumption of normal polling

**Do not reduce `_BACKOFF_THRESHOLD` below 2** — devices may legitimately fail 1-2 reads due to serial bus congestion. The threshold should allow for transient failures before enabling back-off.

---

## LAN push types — how to tell them apart

All pushes arrive as `CMD_DATA_RECV (0x0091)` frames. The payload starts with `action` + 6-byte `attr_flags`.

| Condition | Push type | What it updates |
|---|---|---|
| `flags[0] & 0x10` | Compact telemetry | `mode`, `is_on`, RPMs, voltages, power |
| DP 34 flagged, DPs 35/36 absent | State notify | `timestamp` only — **never `is_on`** |
| DP 18 flagged | Mode update | `mode`, `is_on` |
| DPs 19–22 flagged | Config | `feed_duration`, `model_a/b`, `wash_reminder` |

**DP 34 (Time/state notify) does NOT affect `is_on`.** The `data[0]` byte contains a hardware power flag (wall power, not pump state). Do not use it to derive `is_on`. Pump on/off state comes exclusively from `mode`.

The state-notify debug log must print `hw_power=bool(data[0] & 1)`, not `self._state.is_on`. This was a source of confusing logs where the logged "power" value was always stale from the previous compact-telemetry parse.

---

## Adding or changing push parsing (`api.py`)

- Parse functions (`_parse_compact_telemetry`, `_parse_state_notify`) are pure: they take `data: bytes` and mutate `state`. Keep them pure — no I/O, no logging inside.
- `_process_push` is the only place that decides which parser to call. Logging belongs here.
- `_dp_is_flagged` / `_dp_data_offset` handle the attr_flags bitmap. Use them; do not hardcode byte offsets.
- `DP_LENGTHS` in `const.py` must stay accurate — it drives offset calculation for all non-bool DPs.
- **Model immutability**: `model_a` and `model_b` (DPs 20/21) are hardware characteristics that must never change after initial read. The `_model_initialized` flag in `MaxspectDeviceState` ensures these values are only set once (from either cloud or LAN) and never updated thereafter. This prevents the device model from incorrectly flipping between XF330CE and XF350CE.

---

## Adding or changing control logic (`coordinator.py`)

- All mode writes go through `coordinator.async_set_mode(mode)` → `cloud.async_set_mode`.
- After a successful write, always set `_pending_mode` and `_write_lock_until`. Never skip this.
- If you add a new writable attribute (not just mode), extend the cooldown logic accordingly.
- `async_seed_from_cloud` pulls stale cached data from the cloud on startup. It is a best-effort seed — real state arrives via LAN within seconds of connection. Never treat cloud seed as ground truth.

---

## Testing

Run tests:
```bash
source ha-venv/bin/activate
pytest tests/ -v
```

Install test dependencies if missing:
```bash
pip install pytest pytest-asyncio
```

### Test files

| File | Covers |
|---|---|
| `tests/test_api_parsing.py` | Pure parsing functions — no HA dependency |
| `tests/test_coordinator_behaviour.py` | Race-condition demonstration + write-cooldown fix |

### Test invariants to maintain

- `_parse_state_notify` must **never** set `is_on`. Tests assert this explicitly.
- `_parse_compact_telemetry` with `MODE_FEED` must set `is_on=True` (by design).
- After `async_set_mode(MODE_OFF)`, a stale LAN push during cooldown must not change `coordinator.data.is_on` to `True`.
- After cooldown expires, LAN pushes must be accepted normally.

---

## Diagnostic scripts (`_agent_workdir/`)

- `validate_state_race.py` — connects to the device, logs every LAN push with timestamps, optionally sends a cloud write and shows the race-condition window. Run this against the real device before and after any change to the coordinator's push-handling logic.

Usage:
```bash
python3 _agent_workdir/validate_state_race.py \
  --host 192.168.50.180 \
  --cloud-user EMAIL --cloud-pass PASSWORD --cloud-did DEVICE_DID
```

---

## Development environment

```bash
# Run Home Assistant with the integration loaded
source ha-venv/bin/activate
hass -c ha-config

# Enable debug logging (ha-config/configuration.yaml):
logger:
  default: warning
  logs:
    custom_components.maxspect: debug
```

After code changes: HA UI → Settings → Integrations → Maxspect → ⋮ → Reload (avoids full restart for most changes).