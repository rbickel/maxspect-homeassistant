"""Shared payload builders for Gizwits protocol regression tests."""

from __future__ import annotations

import struct

from custom_components.maxspect.const import ACTION_DEVICE_REPORT, MODE_ON


def build_full_status_payload(mode: int = MODE_ON, hw_power: int = 1) -> bytes:
    """Build a synthetic full report with the preamble observed in the live capture."""
    data = bytearray(1044)
    data[:8] = bytes.fromhex("ffff000000240128")
    data[33:40] = bytes([hw_power, 26, 10, 2, 16, 28, 17])
    data[45] = mode
    struct.pack_into(">H", data, 921, 2715)
    struct.pack_into(">H", data, 923, 2394)
    data[926] = 69
    data[927] = 0xAA
    struct.pack_into(">H", data, 930, 2747)
    struct.pack_into(">H", data, 932, 2377)
    data[935] = 27
    data[936] = 0xBB
    return bytes([ACTION_DEVICE_REPORT]) + b"\xff" * 4 + bytes(data)
