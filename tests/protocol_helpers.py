"""Shared payload builders for Gizwits protocol regression tests."""

from __future__ import annotations

import struct

from custom_components.maxspect.const import ACTION_DEVICE_REPORT, DP_LENGTHS, MODE_ON


def build_full_status_payload(mode: int = MODE_ON, hw_power: int = 1) -> bytes:
    """Build all 47 schema fields, including packed boolean data."""
    telemetry = bytearray(25)
    telemetry[0] = MODE_ON
    struct.pack_into(">H", telemetry, 2, 2715)
    struct.pack_into(">H", telemetry, 4, 2394)
    telemetry[7] = 69
    telemetry[8] = 0xAA
    struct.pack_into(">H", telemetry, 11, 2747)
    struct.pack_into(">H", telemetry, 13, 2377)
    telemetry[16] = 27
    telemetry[17] = 0xBB
    scalars = {17: 36, 18: mode, 19: 10, 20: 0, 21: 0, 22: 30}
    binary = {34: bytes([hw_power, 26, 10, 2, 16, 28, 17]), 44: bytes(telemetry)}
    data = b"\x00" * 3
    for dp in range(17, 47):
        data += (
            bytes([scalars.get(dp, 0)]) if dp < 33
            else binary.get(dp, bytes(DP_LENGTHS[dp]))
        )
    return bytes([ACTION_DEVICE_REPORT]) + b"\xff" * 6 + data
