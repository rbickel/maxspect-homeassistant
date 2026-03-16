# Maxspect XF330CE Gyre — Gizwits LAN Protocol

> Reverse-engineered from live captures against a Maxspect XF330CE Double
> Edition dual gyre pump at **192.168.50.180**.
> Product key: `cd01d1f3ab2647ea9da51e045cf53d61`
> Device ID: `G3C1A0005968`
>
> **Last verified**: 2026-03-14 — three independent capture sessions confirmed
> all timings, field offsets, and push types documented below.

---

## 1. Network Overview

| Item | Value |
|------|-------|
| LAN control port | **TCP 12416** |
| Discovery port | UDP 12414 (broadcast) |
| Discovery response | **None** — the Maxspect does NOT reply to Gizwits UDP discovery broadcasts |
| IP must be known | Yes — enter it manually in the integration config flow |

Because the device ignores UDP 12414, the integration uses a **manual IP**
config flow (`async_step_manual_device`) rather than auto-discovery.

---

## 2. Gizwits LAN Frame Format

Every frame on TCP 12416 follows this structure:

```
┌──────────────┬──────────────┬──────────────────────────────────────┐
│ Header (4 B) │ Length (LEB128) │ Data (length bytes)               │
│ 00 00 00 03  │ variable     │ flag(1) + cmd(2) + payload           │
└──────────────┴──────────────┘──────────────────────────────────────┘
```

- **Header**: always `00 00 00 03`.
- **Length**: [LEB128](https://en.wikipedia.org/wiki/LEB128) encoded unsigned
  integer giving the byte count of the Data section that follows.
- **Data** starts with:
  - `flag` (1 byte) — if bit 0 is set, a DID field follows the cmd.
  - `cmd` (2 bytes, big-endian) — the command code.
  - `[did_len (2 B) + did (did_len B)]` — only present when `flag & 0x01`.
  - Remaining bytes are command-specific payload.

> **Note**: The original Gizwits spec describes an additional `sn(4)` field
> between cmd and payload. The Maxspect XF330CE does **not** use a separate
> SN field at the frame level.

### LEB128 Decoding

```python
length = 0; shift = 0
for byte in stream:
    length |= (byte & 0x7F) << shift
    if (byte & 0x80) == 0: break
    shift += 7
```

---

## 3. Connection Sequence

### 3.1 Handshake (CONFIRMED 2026-03-14)

```
Client                                    Maxspect (192.168.50.180:12416)
  │                                           │
  │  ──► TCP connect ─────────────────────►   │
  │                                           │
  │  ──► 0x0006 (device info request) ───►    │
  │  ◄── 0x0007 (device info response) ◄──   │
  │       └─ payload = binding key (12 B)     │
  │          hex: 00 0a 43 41 54 44 43 4c      │
  │               4f 56 54 45                  │
  │          ASCII: "CATDCLOVTE" (len-prefixed)│
  │                                           │
  │  ──► 0x0008 (bind request) ──────────►    │
  │       └─ payload = binding key (12 B)     │
  │  ◄── 0x0009 (bind ACK) ◄─────────────    │
  │  ◄── 0x0009 (duplicate ACK) ◄────────    │  ← always sent twice
  │                                           │
```

The device always sends the bind ACK **twice**. The second should be
drained and discarded.

### 3.2 Push-Based Data Flow (CONFIRMED 2026-03-14)

After the handshake, the device autonomously pushes data. **No 0x0093
status request is needed to trigger pushes.** The observed push schedule:

```
  │  ◄── 0x0091 push (14 B state notification) ◄──   │  ~15-30s after handshake
  │  ◄── 0x0091 push (1049 B full status)  ◄──────   │  ~60-120s after handshake
  │  ◄── 0x0091 push (32 B compact telemetry) ◄───   │  ~3-5 min intervals
  │  ◄── 0x0091 push (14 B state notification) ◄──   │  on power state changes
  │       ...continues for life of connection...       │
```

### 3.3 Status Request (0x0093) — NOT USEFUL

Sending 0x0093 causes the device to reply with 0x0094 echoing the 4-byte
request ID — but **no actual status data**. The full status push arrives
on the device's own schedule regardless of whether 0x0093 was sent.

```
  │  ──► 0x0093 payload: 00 00 00 02 02 ────►    │
  │  ◄── 0x0094 payload: 00 00 00 02 ◄───────    │  (echo of request ID only)
```

The first 4 bytes of the 0x0093 payload are a request identifier; the 5th
byte `02` is the Gizwits P0 "read status" action. The device echoes only
the 4-byte ID in the 0x0094 response.

### 3.4 Active Polling via 0x0090 Read (CONFIRMED 2026-03-14)

The 0x0090 command with action byte `0x11` acts as an **attribute read
request**. The device responds with a 0x0091 push containing the requested
data segment(s). The payload format is:

```
0x0090 payload: [action=0x11] [0x14] [mask_b1] [mask_b2] [mask_b3] [mask_b4] [mask_b5]
                 └── read     └── attr flag prefix     └── bit mask selecting data segments
```

#### Discovered Data Segments

| Mask Byte/Bit | 0x0090 Payload | Response | Repeatable? | Latency | Content |
|---|---|---|---|---|---|
| b1 bit 2 (0x04) | `11 14 04 00 00 00 00` | 14 B | **Yes — 100%** | **~0.3s** | State notify: power + timestamp |
| b1 bit 3 (0x08) | `11 14 08 00 00 00 00` | 69 B | **Yes — 100%** | ~0.5s | Counter block (mostly zeros) |
| b1 bit 4 (0x10) | `11 14 10 00 00 00 00` | 788 B | **Yes — 100%** | ~1.0s | Schedule/program data region |
| b2 bit 5 (0x20) | `11 14 00 20 00 00 00` | 1049 B | **Once per connection** | ~1.5s | Full status (all sensor data) |

**Key behaviours:**
- **State notify (0x04)** is the only segment reliably pollable at 2s
  intervals — tested 20/20 with avg 0.28s latency.
- **Full status (byte2 0x20)** responds only once per TCP session.
  Subsequent requests to this segment return nothing.
- Combined masks (e.g. `0x14 = 0x04|0x10`) deliver the larger segment
  only on the first request; subsequent requests return only state notify.
- Action byte `0x11` is the only one that triggers data; `0x01`, `0x02`,
  `0x04` only produce empty ACKs.
- The empty ACK (0-byte 0x0091) always precedes the data response.

#### Polling Latencies (measured 2026-03-14, 20 consecutive polls)

```
State notify (0x04):  20/20 success  avg=0.28s  min=0.17s  max=0.42s
69B block    (0x08):  10/10 success  avg=0.55s  min=0.49s  max=0.68s
788B block   (0x10):  10/10 success  avg=1.02s  min=0.97s  max=1.06s
Full status  (0x20):   1/1  success  latency=1.54s (first request only)
```

#### Strategy for Fast Updates

The optimal polling strategy combines one-shot reads with repeatable polls:

1. **On connect**: Request full status (`11 14 00 20 00 00 00`) — gets all
   sensor data (RPM, voltage, power, mode, speed settings) in ~1.5s.
2. **Every 2–3s**: Poll state notify (`11 14 04 00 00 00 00`) — detects
   power state changes and provides a live timestamp.
3. **React to pushes**: The background listener also receives autonomous
   compact telemetry (~3–5 min) and state notifications (on changes),
   which overlay with the polled data.
4. **On disconnect/reconnect**: Full status is read again on each new
   connection, providing a complete refresh of all sensor data.

### 3.5 Push Payload Prefixes (CONFIRMED 2026-03-14)

All 0x0091 push payloads start with action byte `0x14`, followed by a type
indicator:

| Prefix | Total Size | Meaning | Payload after prefix |
|--------|-----------|---------|---------------------|
| `14 ff ff ff ff` | 1049 B | Full status (§5) | 1044-byte status payload |
| `14 10` | 32 B | Compact telemetry (§5.4) | 30-byte sensor summary |
| `14 00 04` | 14 B | State notification (§5.5) | 11 bytes: flags(4) + power(1) + timestamp(6) |
| `14 00 00 00 04` | 8 B | Change notification (§5.6) | 3 bytes: `00 00 XX` |
| `14 00 08` | 69 B | Counter block (§5.7) | 62 bytes, mostly zeros + counter at byte 7 |
| `14 00 10` | 788 B | Schedule region (§5.8) | 781 bytes, schedule/program data |
| `14 00 14` | 795 B | Combined state+schedule | Merged state notify + schedule (combined read) |
| `14 00 0C` | 76 B | Combined state+counter | Merged state notify + counter block |
| _(empty)_ | 0 B | Write ACK (§8) | Sent after 0x0090 write/read commands |

### 3.6 Observed Push Timing

Measured across three independent capture sessions (2026-03-14):

| Push Type | First Arrival | Repeat Interval |
|-----------|---------------|-----------------|
| Full status (1049 B) | 60–120 s after handshake | Not observed to repeat within 8 min |
| Compact telemetry (32 B) | ~110 s after handshake | ~3–5 min |
| State notification (14 B) | ~15 s after handshake | On power state changes |
| Change notification (8 B) | During state transitions | Bursts of 1–5 during changes |

### 3.7 Heartbeat

```
  │  ──► 0x000C (heartbeat request) ─────►   │
  │  ◄── 0x000D (heartbeat response) ◄───    │
  │       ...every 20–30 seconds...            │
```

Client must send heartbeats to keep the TCP connection alive. Heartbeat
frames are interleaved with push frames in the stream.

---

## 4. Command Reference

| Cmd (hex) | Direction | Description |
|-----------|-----------|-------------|
| `0x0006` | Client → Device | Request device info |
| `0x0007` | Device → Client | Device info response (binding key in payload) |
| `0x0008` | Client → Device | Bind/login with binding key |
| `0x0009` | Device → Client | Bind ACK (sent twice by device) |
| `0x000C` | Client → Device | Heartbeat ping |
| `0x000D` | Device → Client | Heartbeat pong |
| `0x0090` | Client → Device | Write/Read attribute (see §3.4 for read, §8 for write) |
| `0x0091` | Device → Client | **Push status** (unsolicited — all data delivered this way) |
| `0x0093` | Client → Device | Status request — device echoes request ID only |
| `0x0094` | Device → Client | Status response — echo of 0x0093 request ID (no data) |

---

## 5. Status Payload — Memory Map

After stripping the Gizwits frame envelope (header + LEB128 + flag + cmd)
and the **push prefix** (5 bytes for full status), the remaining bytes are
the **device status payload**. Full-size payloads are **1044 bytes**.

### 5.1 Confirmed Attributes (verified 2026-03-14)

All offsets are byte offsets into the 1044-byte status payload (0-based).

| Offset (dec) | Offset (hex) | Len | Name | Type | Description |
|---|---|---|---|---|---|
| 0–20 | 0x00–0x14 | 21 | _header_ | — | Preamble: `ff ff 00 00 00 24 01 1e` + zeros |
| 21–32 | 0x15–0x20 | 12 | DeviceID | ASCII | `G3C1A0005968` |
| **33** | **0x21** | **1** | **Power** | **bool** | **`0x01` = ON, `0x00` = OFF** |
| 34–39 | 0x22–0x27 | 6 | Timestamp | binary | `YY MM DD HH MM SS` |
| 40–42 | 0x28–0x2A | 3 | _constant_ | — | `08 01 01` — same across all captures |
| **43** | **0x2B** | **1** | **MinSpeed** | **uint8** | **Min speed %. Observed: 10** |
| **44** | **0x2C** | **1** | **MaxSpeed** | **uint8** | **Max speed %. Observed: 68** |
| **45** | **0x2D** | **1** | **Mode** | **uint8** | **Operating mode. Observed: 5** |
| 46 | 0x2E | 1 | _unknown_ | — | Observed: `0x01` |
| **47** | **0x2F** | **1** | **Interval** | **uint8** | **Wave interval in seconds. Observed: 10** |
| 48–101 | 0x30–0x65 | 54 | _padding_ | — | All zeros |
| 102–154 | 0x66–0x9A | ~53 | ModeBlock | binary | Mode/program definition block (§5.2) |
| 155–884 | 0x9B–0x374 | 730 | _reserved_ | — | Mostly zeros |
| 884–885 | 0x374–0x375 | 2 | _unknown_ | uint16 BE? | Observed: `0x1D35` = 7477. Purpose unknown. |
| 886–920 | 0x376–0x398 | 35 | _reserved_ | — | All zeros |
| **921–922** | **0x399** | **2** | **CH1_RPM** | **uint16 BE** | **Channel 1 RPM** |
| **923–924** | **0x39B** | **2** | **CH1_Voltage** | **uint16 BE** | **÷ 100 = volts (e.g. 2412 → 24.12 V)** |
| 925 | 0x39D | 1 | _zero_ | — | Always `0x00` |
| **926** | **0x39E** | **1** | **CH1_Power** | **uint8** | **Channel 1 power in watts** |
| 927 | 0x39F | 1 | _marker_ | — | Always `0xAA` — channel delimiter |
| 928–929 | 0x3A0–0x3A1 | 2 | _unknown_ | — | `0x05 0x00` |
| **930–931** | **0x3A2** | **2** | **CH2_RPM** | **uint16 BE** | **Channel 2 RPM** |
| **932–933** | **0x3A4** | **2** | **CH2_Voltage** | **uint16 BE** | **÷ 100 = volts** |
| 934 | 0x3A6 | 1 | _zero_ | — | Always `0x00` |
| **935** | **0x3A7** | **1** | **CH2_Power** | **uint8** | **Channel 2 power in watts** |
| 936 | 0x3A8 | 1 | _marker_ | — | Always `0xBB` — channel delimiter |
| 937–938 | 0x3A9–0x3AA | 2 | _unknown_ | — | `0x0B 0x0B` |
| 939–1043 | 0x3AB–0x413 | 105 | _tail_ | — | All zeros |

### 5.2 Mode Block (offset 102 / 0x66)

The mode block starts with a length byte followed by mode/program definitions.

```
Byte 0:  length of the block (e.g. 0x31 = 49 bytes)
Byte 1:  program count or sub-mode selector (0x04)
Byte 2+: pairs of (speed, interval?) definitions for each program step
```

Example (ON, mode 5, captured 2026-03-14):
```
  0066: 31 04 01 00 00 04 01 05 06 01 05 02 06 00 04 01
  0076: 08 06 01 08 03 0a 00 03 01 0a 4a 4a 00 0a 44 44
  0086: 03 01 0a 4a 4a 00 0a 44 44 04 12 00 04 01 08 05
  0096: 01 08 00 00 00 00 00 00 00 00
```

Identical across all captures with the same mode setting. The exact
encoding of each program step is not yet fully decoded.

### 5.3 Full Status — Verified Hex Dump (2026-03-14)

```
0000: ff ff 00 00 00 24 01 1e 00 00 00 00 00 00 00 00   Header
0010: 00 00 00 00 00 47 33 43 31 41 30 30 30 35 39 36   DeviceID
0020: 38 01 1a 03 0e 0b 2a 29 08 01 01 0a 44 05 01 0a   Pwr=ON Ts Mode=5
 ...  (offsets 48-920: mode block + zeros)
0398: 00 00 00 00 00 00 00 05 00 07 db 09 6c 00 0d aa   CH1: 2011rpm 24.12V 13W
03a0: 05 00 09 a7 09 57 00 17 bb 0b 0b 00 00 00 00 00   CH2: 2471rpm 23.91V 23W
```

### 5.4 Compact Telemetry Push (32 bytes total)

The device sends 32-byte compact telemetry pushes every ~3–5 minutes.
After stripping the 2-byte prefix (`14 10`), the 30-byte payload is:

| Offset | Len | Name | Type | Description |
|--------|-----|------|------|-------------|
| 0–4 | 5 | _header_ | — | Zeros |
| 5 | 1 | Mode | uint8 | Operating mode (same as full status offset 45) |
| 6 | 1 | _zero_ | — | Always `0x00` |
| 7–8 | 2 | CH1_RPM | uint16 BE | Channel 1 RPM |
| 9–10 | 2 | CH1_Voltage | uint16 BE | Voltage × 100 |
| 11 | 1 | _zero_ | — | Always `0x00` |
| 12 | 1 | CH1_Power | uint8 | Channel 1 power in watts |
| 13 | 1 | _marker_ | — | Always `0xAA` |
| 14–15 | 2 | _unknown_ | — | `0x05 0x00` |
| 16–17 | 2 | CH2_RPM | uint16 BE | Channel 2 RPM |
| 18–19 | 2 | CH2_Voltage | uint16 BE | Voltage × 100 |
| 20 | 1 | _zero_ | — | Always `0x00` |
| 21 | 1 | CH2_Power | uint8 | Channel 2 power in watts |
| 22 | 1 | _marker_ | — | Always `0xBB` |
| 23–24 | 2 | _unknown_ | — | `0x0B 0x0B` |
| 25–29 | 5 | _tail_ | — | All zeros |

Example (2026-03-14):
```
14 10 00 00 00 00 00 05 00 0a cb 09 67 00 21 aa 05 00 0a ce 09 54 00 1e bb 0b 0b 00 00 00 00 00
            └─header─┘  │     └── CH1: 2763rpm 24.07V 33W       └── CH2: 2766rpm 23.88V 30W
                        └─ mode=5
```

### 5.5 State Notification Push (14 bytes total)

Sent on power state changes. Prefix `14 00 04`, then 11 bytes:

```
14 00 04 00 00 00 00 01 1a 03 0e 0b 17 13
         └──flags──┘  │  └─ timestamp: YY MM DD HH MM SS (2026-03-14 11:23:19)
                      └── power: 0x01=ON, 0x00=OFF
```

| Offset (after prefix) | Len | Description |
|------------------------|-----|-------------|
| 0–3 | 4 | Flags (observed: all zeros) |
| 4 | 1 | Power: `0x01` = ON, `0x00` = OFF |
| 5–10 | 6 | Timestamp: YY MM DD HH MM SS |

### 5.6 Change Notification Push (8 bytes total)

Sent in bursts during state transitions (e.g. power toggle). Prefix
`14 00 00 00 04`, then 3 bytes:

```
14 00 00 00 04 00 00 03    ← observed during OFF transitions
14 00 00 00 04 00 00 01    ← observed during ON transitions / keepalive
```

The last byte appears to indicate the type of change (`01` or `03`).
These frames carry no sensor data and can be safely ignored.

### 5.7 Counter Block (69 bytes, prefix `14 00 08`)

Returned by `0x0090` read with mask bit `0x08`. Contains 62 bytes of
payload, mostly zeros except for a sequence counter at offset 7:

```
14 00 08 00 00 00 00 XX 00 00 00 ...
                     └── counter (increments by ~1/s)
```

This block contains no useful sensor data. The counter value is an
internal device sequence number. Reliably pollable at 2s intervals.

### 5.8 Schedule Region (788 bytes, prefix `14 00 10`)

Returned by `0x0090` read with mask bit `0x10`. Contains schedule/program
data, mostly zeros when no programs are actively running. Reliably
pollable at ~1s intervals.

```
14 00 10 00 00 00 00 XX 00 00 ...  (788 total bytes)
                     └── sequence counter
```

The non-zero data appears to contain mode/program definitions similar to
the mode block in the full status (§5.2). When the pump is off, this
region is all zeros except for the header and counter.

---

## 6. Verified Sensor Readings Across Captures

| Capture | Elapsed | Type | Power | Mode | CH1 RPM | CH1 V | CH1 W | CH2 RPM | CH2 V | CH2 W |
|---------|---------|------|-------|------|---------|-------|-------|---------|-------|-------|
| #1 | 16.8s | Compact | ON | 5 | 2763 | 24.07 | 33 | 2766 | 23.88 | 30 |
| #2 | 197.7s | Compact | ON | 5 | 2539 | 24.09 | 24 | 2739 | 23.90 | 29 |
| #3 | 108.6s | Full | ON | 5 | 2011 | 24.12 | 13 | 2471 | 23.91 | 23 |
| #3 | 400.6s | Compact | ON | 5 | 2011 | — | 13 | 2471 | — | 23 |

RPM varies over time due to alternating gyre mode. Voltage is stable ~24V.

---

## 7. Binding Key

The binding key is the full 12-byte payload of the 0x0007 response:

```
Hex:   00 0a 43 41 54 44 43 4c 4f 56 54 45
        └─ length prefix (0x000A = 10 bytes of ASCII)
ASCII:       C  A  T  D  C  L  O  V  T  E
```

The full 12 bytes (including length prefix) are echoed back as the
0x0008 bind payload.

---

## 8. Writing and Reading Attributes (0x0090)

### 8.1 Reading Attributes (action=0x11)

The 0x0090 command with action byte `0x11` reads device data segments.
See §3.4 for full details. Summary:

```
Client ──► 0x0090 [0x11 0x14 mask_b1 mask_b2 mask_b3 mask_b4 mask_b5] ──►
Device ◄── 0x0091 (empty ACK) + 0x0091 (data response)              ◄──
```

Example — read state notify:
```
TX:  0x0090 payload = 11 14 04 00 00 00 00
RX:  0x0091 payload = (empty)
RX:  0x0091 payload = 14 00 04 00 00 00 00 01 0b 0c 09 00 03 11
```

### 8.2 Writing Attributes (DISCOVERED 2026-03-15)

Analysis of the decompiled **Syna-G+ Cloud v1.6.4** APK revealed the
Gizwits product definition and app-level control flow. The device uses
**Gizwits V4 `var_len`** protocol (`packetVersion: 0x00000004`).

#### Product Data Points (from APK `resources/assets/USapi/<product_key>.json`)

The product defines **47 data points** (IDs 0–46) across three types:

| ID | Name | Type | Data Type | Len | Writable | Description |
|----|------|------|-----------|-----|----------|-------------|
| 0 | Upgrade_License | bool | bit | 1 | W | 0=allow, 1=deny |
| 1 | Reboot | bool | bit | 1 | W | 0=noop, 1=reboot |
| 2 | Factory_Settings | bool | bit | 1 | W | 0=noop, 1=factory reset |
| 3 | State_A | bool | bit | 1 | Alert | 0=connected, 1=disconnected |
| 4 | State_B | bool | bit | 1 | Alert | 0=connected, 1=disconnected |
| 5 | Error_A | bool | bit | 1 | Alert | 0=normal, 1=error |
| 6 | Error_B | bool | bit | 1 | Alert | 0=normal, 1=error |
| 7–11 | Bak1–Bak5 | bool | bit | 1 | W | Reserved |
| 12–16 | Bak6–Bak10 | bool | bit | 1 | Alert | Reserved |
| 17 | Version_Firmware | uint8 | byte | 1 | R | e.g. 11 → v1.1 |
| **18** | **Mode** | **uint8** | **byte** | **1** | **W** | **0=water flow, 1=programming, 2=feed, 3=OFF, 4=exit feed, 5=ON** |
| 19 | Time_Feed | uint8 | byte | 1 | W | 5–120 minutes |
| 20 | Model_A | uint8 | byte | 1 | W | 0=330, 1=350 |
| 21 | Model_B | uint8 | byte | 1 | W | 0=330, 1=350 |
| 22 | Wash | uint8 | byte | 1 | W | Daily reminder countdown |
| 23–32 | Bak11–Bak20 | uint8 | byte | 1 | W | Reserved |
| 33 | Serial_Number | binary | 12 B | 12 | R | Device serial number |
| 34 | Time | binary | 7 B | 7 | W | YY MM DD HH MM SS + flag |
| 35 | Manual | binary | 62 B | 62 | W | Manual pump program data |
| 36 | Auto | binary | 781 B | 781 | W | Auto/scheduled program data |
| 37 | Countdown_Feed | binary | 3 B | 3 | W | Feed countdown timer |
| 38 | Backup | binary | 4 B | 4 | W | Pump A/B direction+speed |
| 39 | Current_A | binary | 2 B | 2 | W | Pump A current |
| 40 | Current_B | binary | 2 B | 2 | W | Pump B current |
| 41–46 | Bak21–Bak26 | binary | 5-50 B | var | W | Reserved |

> **All `byte_offset` values = 0** in the product definition — standard for
> Gizwits V4 `var_len` where data is packed dynamically by attr_flags.

#### Mode Data Point — Power Control

The **Mode** data point (ID 18) controls the device operating state:

| Mode Value | Meaning | Description |
|------------|---------|-------------|
| 0 | Water Flow | Manual pump mode — pumps running at set parameters |
| 1 | Programming | Schedule/auto mode active |
| 2 | Feed | Feeding mode — pumps paused for programmed duration |
| **3** | **OFF** | **Power OFF — pumps stopped** |
| 4 | Exit Feed | Resume normal operation from feeding mode |
| **5** | **ON** | **Power ON — resume last active program** |

#### App Control Flow (from CoolShadowControlActivity)

The decompiled app sends power commands via the Gizwits SDK:
```java
// Power ON:
sendCommand(device, "Mode", sn=10, value=5);
// Power OFF:
sendCommand(device, "Mode", sn=10, value=3);
```

Which calls `GizWifiDevice.write({"Mode": 5}, sn)` → Gizwits daemon
translates to P0 binary → TCP frame on port 12416.

#### P0 Write Frame Format (Gizwits V4 var_len)

The 0x0090 frame payload for writes uses the standard Gizwits V4 format:

```
0x0090 payload: [action] [attr_flags (6 bytes)] [data_values...]
```

Where:
- **action** = `0x12` (extended write — mirrors `0x11` extended read)
- **attr_flags** = 6-byte bitmask (48 bits for 47 data points, **big-endian byte order**)
  - byte[0] = DPs 40-47, byte[5] = DPs 0-7
  - byte_idx = 5 - (dp_id ÷ 8), bit_idx = dp_id % 8
  - For bool DPs: the flag BIT is the VALUE (1=true, 0=false)
  - For non-bool DPs: a set bit means data follows
- **data_values** = values for flagged non-bool DPs, in ascending ID order

For **Mode=5** (power ON), Mode is data point ID 18:
- byte_idx = 5 - (18 ÷ 8) = 5 - 2 = **3**, bit 2 (18 % 8) → `0x04`
- attr_flags: `00 00 00 04 00 00`
- data: `05`

```
Write Mode=5 (ON):  0x0090 payload = 12 00 00 00 04 00 00 05
Write Mode=3 (OFF): 0x0090 payload = 12 00 00 00 04 00 00 03
```

> **✅ VERIFIED 2026-03-15** on live device: pwr byte flips 0x01→0x00 on OFF,
> 1050B write-ACK (action=0x13) returned on ON. CHANGE_NOTIFY fires for both.
> Note: OFF may cause device reboot (timestamps reset).

---

## 9. Power State Staleness (CONFIRMED 2026-03-14)

### 9.1 Problem: Polled State Notify Returns Stale Power Data

The state notify poll (`0x0090` with mask `0x04`) **caches the power value
indefinitely** after a power toggle. The device does not update the polled
value to reflect the actual hardware state — it returns whatever value was
last written to its internal cache.

**Definitive test (2026-03-14)**: 30 consecutive fresh-connection polls
over 8+ minutes, ALL returned `power=ON` even though the pump was
physically OFF for 4+ minutes. The stale value persisted across new TCP
connections and fresh bind requests.

```
Poll #1  → power=ON  (pump ON)
Poll #14 → power=OFF (pump toggled OFF at this exact moment — caught briefly)
Poll #15 → power=ON  (stale!)
Poll #16 → power=ON  (stale!)
 ...
Poll #30 → power=ON  (stale! — pump has been OFF for 4+ minutes)
```

### 9.2 Solution: CHANGE_NOTIFY-Gated Power State

The **only reliable signal** for power state changes is the autonomous
push sequence that occurs during a real power transition on a persistent
connection:

```
Device ──► 0x0091 payload = 14 00 00 00 04 ...   (CHANGE_NOTIFY)
Device ──► 0x0091 payload = 14 00 04 ...          (STATE_NOTIFY with correct power)
```

The CHANGE_NOTIFY (`14 00 00 00 04`) **always** precedes an autonomous
STATE_NOTIFY during a real power transition. Polled STATE_NOTIFY responses
never include a preceding CHANGE_NOTIFY.

**Gate logic** (implemented in `api.py`):
1. When `CHANGE_NOTIFY` arrives → set `_change_notify_seen = True`
2. When `STATE_NOTIFY` arrives with a **different** power value:
   - If `_change_notify_seen` is `False` → **reject** (stale poll data)
   - If `_change_notify_seen` is `True` → **accept** (real transition)
   - Clear `_change_notify_seen` only after accepting a power change
3. When `STATE_NOTIFY` arrives with the **same** power value → always accept
   (no conflict, preserves the CHANGE_NOTIFY flag for the next frame)

**Race condition note**: The flag must only be cleared when actually
consuming an authoritative power *change*, not on every STATE_NOTIFY.
Otherwise a non-changing poll response arriving between CHANGE_NOTIFY
and the real autonomous push would consume the flag.

### 9.3 What Polls Are Still Useful For

Despite power staleness, the state notify poll is still valuable:
- **Timestamp**: The polled timestamp is always current (device clock)
- **Liveness**: A successful poll confirms the TCP connection is alive
- **Non-power data**: Mode, speed, RPM etc. from full status and compact
  telemetry pushes are accurate — only the power byte in state_notify is
  affected by the staleness bug

---

## 10. DANGER: Unknown 0x0090 Mask Bits Can Factory-Reset the Device

> **⚠️ CRITICAL WARNING — READ BEFORE ANY PROTOCOL EXPERIMENTATION ⚠️**

### 10.1 Incident (2026-03-14)

A probe script sent **20 unknown single-bit mask combinations** via
`CMD_WRITE_CTRL` (0x0090) with action byte `0x11` (read). Despite `0x11`
being a read-only action, the device performed a **full factory reset**:
- WiFi configuration wiped (device went offline, unreachable by ping)
- App binding lost (device no longer paired in Maxspect app)
- All pump settings reset to defaults
- Required physical power cycle and complete re-setup

### 10.2 What Was Sent

The probe swept all 8 bits in each of 3 mask bytes, each on a fresh
connection with a new bind request (24+ connections total):

```
Payload format: [0x11] [0x14] [byte1] [byte2] [byte3] [0x00] [0x00]

Safe masks (known and tested):
  byte1=0x04 → state_notify (14B)    ✓ safe
  byte1=0x08 → counter block (69B)   ✓ safe
  byte1=0x10 → schedule region (788B) ✓ safe
  byte2=0x20 → full status (1049B)   ✓ safe

DANGEROUS — triggered factory reset when tested:
  byte1: 0x01, 0x02, 0x20, 0x40, 0x80
  byte2: 0x01, 0x02, 0x04, 0x08, 0x10, 0x40, 0x80
  byte3: 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80
```

The exact trigger bit is unknown — any one of these 20 masks (or the
cumulative effect of 24+ rapid bind requests) caused the reset.

**Byte 3 bits are the most suspicious** — this region was completely
uncharted and all 8 bits were tested for the first time.

### 10.3 Rules for Future Protocol Work

1. **NEVER blind-sweep unknown mask bits** — even with action `0x11` (read)
2. **NEVER create fresh connections for each probe** — use a single
   persistent connection. Rapid bind flooding (24+) can evict the app's
   binding and potentially crash the device's WiFi module
3. **Only use known-safe masks**: `0x04`, `0x08`, `0x10` (byte1) and `0x20` (byte2)
4. **Test ONE new mask at a time** on a dedicated session, with full
   awareness that it may factory-reset the device
5. **Always confirm device is still accessible in the app** after any
   protocol experimentation

---

## 11. Integration Architecture

### 11.1 Hybrid Push+Poll Listener Flow (`api.py`)

```
MaxspectClient
  ├── async_connect()
  │     ├── TCP connect to host:12416
  │     ├── Handshake: 0x0006 → 0x0007 → 0x0008 → 0x0009
  │     ├── Drain duplicate bind ACK
  │     ├── Request full status: 0x0090 [11 14 00 20 00 00 00]
  │     │     └── Receives 1049B full status (once per connection)
  │     └── Start background _listen_loop()
  │
  ├── _listen_loop() [background task]
  │     ├── Read frames continuously
  │     ├── Process 0x0091 pushes → update self._state
  │     │     ├── 14 ff ff ff ff → _parse_full_status() (1044 B)
  │     │     ├── 14 10          → _parse_compact_telemetry() (30 B)
  │     │     ├── 14 00 04       → _parse_state_notify() (11 B)
  │     │     └── other          → ignored
  │     ├── Poll state notify every 2-3s: 0x0090 [11 14 04 00 00 00 00]
  │     │     └── Returns 14B state notify (power + timestamp)
  │     ├── Send heartbeats every 20-30s
  │     └── Notify coordinator via callback on each state update
  │
  └── async_request_status() → return latest self._state
```

### 11.2 Model File

`models/cd01d1f3ab2647ea9da51e045cf53d61.json`

Key properties:
- `product_key`: `cd01d1f3ab2647ea9da51e045cf53d61`
- `push_based`: `true` — device pushes data autonomously
- `attrs[]`: Attribute definitions with byte offsets into full status

### 11.3 Config Flow

```
async_step_user()           → Menu: "Cloud Login" | "Add device by IP"
async_step_manual_device()  → IP + device type dropdown + optional name
                              → validate_lan_device() → create entry
```

---

## 10. What Still Needs Testing

| Item | Status | Notes |
|---|---|---|
| Full status push | ✅ Confirmed | 1049 B, arrives 60-120s after handshake |
| Compact telemetry | ✅ Confirmed | 32 B, every ~3-5 min |
| State notification | ✅ Confirmed | 14 B, on power changes |
| All sensor offsets | ✅ Confirmed | RPM, voltage, power for both channels |
| Binding key format | ✅ Confirmed | 12 B: len(2) + "CATDCLOVTE"(10) |
| Heartbeat | ✅ Confirmed | 0x000C → 0x000D, every 20-30s |
| 0x0093 status request | ✅ Confirmed useless | Only echoes request ID via 0x0094 |
| 0x0090 attr read (0x11) | ✅ Confirmed | State notify pollable at 2s; full status once per connection |
| 0x0090 state notify poll | ✅ Confirmed | 20/20 success, avg 0.28s, bit mask 0x04 |
| 0x0090 full status read | ✅ Confirmed | Once per connection, bit mask byte2 0x20 |
| LAN write via 0x0090 | ⚠️ Format discovered | APK reveals Mode=3(OFF)/5(ON) via attr_flags; binary format derived but untested |
| Power ON/OFF via app | ✅ Observed | Mode=5=ON, Mode=3=OFF (from APK product definition) |
| MinSpeed / MaxSpeed write | ❌ Not tested | App sends via Manual data point (ID 35, 62B binary) |
| Mode selection write | ⚠️ Partially | Mode values: 0=waterflow, 1=programming, 2=feed, 3=off, 4=exit_feed, 5=on |
| Interval write | ❌ Not tested | Part of Manual data point payload |
| Feeding mode | ⚠️ Discovered | Mode=2 activates feed, Time_Feed (ID 19) sets duration 5-120min |
| Full status repeat | ❓ Unclear | Observed once per session; may not repeat |
| Change notification (8 B) | ✅ Mapped | `14 00 00 00 04` prefix, during transitions |
| UDP discovery | ❌ Doesn't respond | Device ignores UDP 12414 broadcasts |

---

## 11. Capture Scripts

| Script | Purpose |
|--------|---------|
| `fresh_capture.py` | Full capture with hex dumps, saves all frames to `fresh_dumps/` |
| `fresh_capture_2.py` | 5-phase protocol test (passive → trigger → variants → writes → passive) |
| `timing_test.py` | Passive listener to measure push intervals |
| `get_full_status.py` | Captures one full status push and decodes all fields |
| `poll_probe.py` | Initial poll probe testing 8 strategies (action bytes, burst, reads, etc.) |
| `poll_probe_deep.py` | Deep dive on action 0x11: repeat test + action range 0x10-0x16 |
| `poll_cooldown_test.py` | Cooldown timing test between trigger responses |
| `poll_reliability_test.py` | Reliability test: full status vs state notify at 2s intervals |
| `poll_attrflags_test.py` | Systematic bit-by-bit test of all attr flag positions |
| `poll_combined_test.py` | Combined attr flags, 69B/788B decode, state notify analysis |
| `poll_final_test.py` | Final validation: 20-poll full status vs state notify |
| `poll_reconnect_test.py` | Fresh-connection-per-poll approach test |

---

## 12. Raw Dump Files

| Directory / File | Contents |
|-----------|----------|
| `fresh_dumps/` | Capture #1: handshake + 0x0093 trigger + 3 min listen |
| `fresh_dumps/full_status.bin` | Raw 1049-byte full status push (with prefix) |
| `fresh_dumps/full_status_data.bin` | Raw 1044-byte status payload (prefix stripped) |
| `fresh_dumps_2/` | Capture #2: 5-phase protocol test |
| `dumps/` | Legacy captures from 2026-03-08 (same patterns) |
