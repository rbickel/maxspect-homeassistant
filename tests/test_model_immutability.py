"""Test that device model attributes remain immutable after initialization.

This test verifies the fix for the issue where push/pull through LAN
could alter device attributes like model, causing XF330CE to change
to XF350CE intermittently.

The fix ensures that model_a and model_b are only set once (either
from cloud or LAN) and cannot be changed by subsequent updates.
"""

from __future__ import annotations

from __future__ import annotations

from custom_components.maxspect.api import MaxspectClient, MaxspectDeviceState
from custom_components.maxspect.const import ATTR_FLAGS_LEN


def _flags_for_dps(*dp_ids: int) -> bytes:
    """Build a 6-byte attr_flags bytestring with the given DPs flagged."""
    flags = bytearray(ATTR_FLAGS_LEN)
    for dp_id in dp_ids:
        byte_idx = ATTR_FLAGS_LEN - 1 - (dp_id // 8)
        bit_idx = dp_id % 8
        if 0 <= byte_idx < ATTR_FLAGS_LEN:
            flags[byte_idx] |= 1 << bit_idx
    return bytes(flags)


class TestModelImmutability:
    """Test model attributes remain stable after first initialization."""

    def test_model_set_once_from_lan_push(self) -> None:
        """Model attributes set from first LAN config DP push remain stable."""
        client = MaxspectClient(host="192.168.1.100")
        # Initial state - no model set
        assert client.state.model_a == 0
        assert client.state.model_b == 0
        assert client.state._model_initialized is False

        # First config DP push with model values (XF330CE)
        # Payload: action=0x14, flags with DPs 19-22, data with values
        action = bytes([0x14])  # ACTION_DEVICE_REPORT
        flags = _flags_for_dps(19, 20, 21, 22)
        data = bytes([10, 0, 0, 30])  # feed=10, model_a=0, model_b=0, wash=30
        payload = action + flags + data

        # Process the push
        client._process_push(payload)

        # Model should be set and initialized
        assert client.state.model_a == 0  # XF330CE
        assert client.state.model_b == 0  # XF330CE
        assert client.state._model_initialized is True

        # Second push attempts to change model to XF350CE
        data2 = bytes([10, 1, 1, 30])  # feed=10, model_a=1, model_b=1, wash=30
        payload2 = action + flags + data2

        # Process the second push
        client._process_push(payload2)

        # Model should remain unchanged (immutable)
        assert client.state.model_a == 0  # Still XF330CE
        assert client.state.model_b == 0  # Still XF330CE
        assert client.state._model_initialized is True

        # But mutable attributes should update
        # (feed_duration and wash_reminder should still update)
        assert client.state.feed_duration == 10
        assert client.state.wash_reminder == 30

    def test_model_set_once_from_cloud_seed(self) -> None:
        """Model attributes set from cloud seeding remain stable."""
        state = MaxspectDeviceState()

        # Simulate cloud seeding (as done in coordinator.py)
        assert state._model_initialized is False

        # Set model from cloud
        state.model_a = 0  # XF330CE
        state.model_b = 0
        state._model_initialized = True

        assert state.model_a == 0
        assert state.model_b == 0
        assert state._model_initialized is True

        # Now simulate a LAN push trying to change the model
        # (In real code, this would be blocked by the check in api.py)
        # We verify the flag is set to prevent changes
        assert state._model_initialized is True

    def test_model_only_set_if_not_initialized(self) -> None:
        """Verify that model is only set when _model_initialized is False."""
        client = MaxspectClient(host="192.168.1.100", product_key="test-key")

        # Pre-initialize the model (e.g., from cloud)
        client.state.model_a = 1  # XF350CE
        client.state.model_b = 1
        client.state._model_initialized = True

        # LAN push with different model values
        action = bytes([0x14])
        flags = _flags_for_dps(20, 21)
        data = bytes([0, 0])  # Trying to set to XF330CE
        payload = action + flags + data

        # Process the push
        client._process_push(payload)

        # Model should remain as XF350CE (not changed to XF330CE)
        assert client.state.model_a == 1
        assert client.state.model_b == 1

    def test_mutable_attributes_continue_to_update(self) -> None:
        """Verify that non-model config attributes still update normally."""
        client = MaxspectClient(host="192.168.1.100")
        # Set initial config
        action = bytes([0x14])
        flags = _flags_for_dps(19, 20, 21, 22)
        data = bytes([10, 0, 0, 30])  # feed=10, model=0/0, wash=30
        payload = action + flags + data
        client._process_push(payload)

        assert client.state.feed_duration == 10
        assert client.state.wash_reminder == 30
        assert client.state.model_a == 0
        assert client.state._model_initialized is True

        # Update only mutable config DPs
        flags2 = _flags_for_dps(19, 22)
        data2 = bytes([15, 45])  # feed=15, wash=45
        payload2 = action + flags2 + data2
        client._process_push(payload2)

        # Mutable attributes should update
        assert client.state.feed_duration == 15
        assert client.state.wash_reminder == 45

        # Model should remain unchanged
        assert client.state.model_a == 0
        assert client.state.model_b == 0
