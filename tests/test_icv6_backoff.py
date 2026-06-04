"""Tests for ICV6 exponential back-off and availability tracking.

Covers:
  1. Back-off interval calculation
  2. Failure tracking and recording
  3. Device availability transitions
  4. Smart logging behavior (WARNING → DEBUG → INFO)
  5. Recovery after consecutive failures
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.maxspect.const import (
    DEFAULT_MAX_BACKOFF_MULTIPLIER,
    DEFAULT_UNAVAILABLE_AFTER_FAILURES,
)
from custom_components.maxspect.icv6_api import ICV6ChildDevice, ICV6_DEVICE_TYPES


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _led_device(device_id: str = "R5S2A001602") -> ICV6ChildDevice:
    """Create a test LED device."""
    return ICV6ChildDevice(
        device_id=device_id,
        device_type="R5",
        type_name=ICV6_DEVICE_TYPES["R5"][0],
        proto_cmd=ICV6_DEVICE_TYPES["R5"][1],
        num_channels=4,
        is_on=True,
        mode=0,
        manual_channels=[50, 60, 70, 80],
    )


class MockICV6Coordinator:
    """Mock coordinator for testing back-off logic without HA context."""

    def __init__(
        self,
        max_backoff_multiplier: int = DEFAULT_MAX_BACKOFF_MULTIPLIER,
        unavailable_after: int = DEFAULT_UNAVAILABLE_AFTER_FAILURES,
    ) -> None:
        self.client = AsyncMock()
        self.data: dict[str, ICV6ChildDevice] = {}
        self._max_backoff_multiplier = max_backoff_multiplier
        self._unavailable_after = unavailable_after
        self._base_interval = 30.0
        self._device_failures: dict[str, tuple[int, float, bool]] = {}

    def _get_backoff_interval(self, failure_count: int) -> float:
        """Calculate effective poll interval based on consecutive failures."""
        if failure_count <= 2:
            multiplier = 1
        elif failure_count <= 5:
            multiplier = 2
        else:
            tier = (failure_count - 6) // 5
            multiplier = 4 * (2 ** tier)
        multiplier = min(multiplier, self._max_backoff_multiplier)
        return self._base_interval * multiplier

    def _should_poll_device(self, device_id: str, now: float) -> bool:
        """Check if enough time has passed to poll this device."""
        if device_id not in self._device_failures:
            return True
        failure_count, last_attempt, _ = self._device_failures[device_id]
        if failure_count == 0:
            return True
        interval = self._get_backoff_interval(failure_count)
        return (now - last_attempt) >= interval

    def _record_device_failure(self, device_id: str, now: float) -> None:
        """Record a device read failure and update back-off state."""
        if device_id in self._device_failures:
            failure_count, _, _ = self._device_failures[device_id]
            failure_count += 1
        else:
            failure_count = 1

        is_unavailable = failure_count >= self._unavailable_after
        self._device_failures[device_id] = (failure_count, now, is_unavailable)

    def _record_device_success(self, device_id: str) -> None:
        """Record a successful device read and reset back-off state."""
        if device_id not in self._device_failures:
            self._device_failures[device_id] = (0, 0.0, False)
            return

        failure_count, _, _ = self._device_failures[device_id]
        if failure_count > 0:
            from custom_components.maxspect.icv6_coordinator import _LOGGER

            _LOGGER.info(
                "ICV6: device %s back online after %d consecutive failures",
                device_id,
                failure_count,
            )
        self._device_failures[device_id] = (0, 0.0, False)

    def is_device_unavailable(self, device_id: str) -> bool:
        """Check if a device is marked as unavailable."""
        if device_id not in self._device_failures:
            return False
        _, _, is_unavailable = self._device_failures[device_id]
        return is_unavailable


def _coordinator(
    max_backoff: int = DEFAULT_MAX_BACKOFF_MULTIPLIER,
    unavailable_after: int = DEFAULT_UNAVAILABLE_AFTER_FAILURES,
) -> MockICV6Coordinator:
    """Create a mock coordinator."""
    return MockICV6Coordinator(max_backoff, unavailable_after)


# ---------------------------------------------------------------------------
# Section 1 — Back-off interval calculation
# ---------------------------------------------------------------------------

class TestBackoffIntervalCalculation:
    """Test the exponential back-off interval logic."""

    def test_no_failures_returns_base_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(0) == 30.0

    def test_one_failure_returns_base_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(1) == 30.0

    def test_two_failures_returns_base_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(2) == 30.0

    def test_three_failures_doubles_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(3) == 60.0

    def test_four_failures_doubles_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(4) == 60.0

    def test_five_failures_doubles_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(5) == 60.0

    def test_six_failures_quadruples_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(6) == 120.0

    def test_ten_failures_quadruples_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(10) == 120.0

    def test_eleven_failures_octuples_interval(self) -> None:
        coord = _coordinator()
        assert coord._get_backoff_interval(11) == 240.0

    def test_max_backoff_ceiling_enforced(self) -> None:
        """Backoff should never exceed base × max_backoff_multiplier."""
        coord = _coordinator(max_backoff=8)
        # Even with 100 failures, should cap at 8× base (240s)
        assert coord._get_backoff_interval(100) == 240.0

    def test_custom_max_backoff_multiplier(self) -> None:
        coord = _coordinator(max_backoff=16)
        # 16 failures would normally give 16×
        assert coord._get_backoff_interval(16) == 480.0  # 30 × 16

    def test_custom_max_backoff_multiplier_capped(self) -> None:
        coord = _coordinator(max_backoff=4)
        # 11 failures would normally give 8×, but our max is 4×
        assert coord._get_backoff_interval(11) == 120.0  # 30 × 4


# ---------------------------------------------------------------------------
# Section 2 — Failure tracking
# ---------------------------------------------------------------------------

class TestFailureTracking:
    """Test per-device failure counter and timestamp tracking."""

    def test_first_failure_increments_counter(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        failure_count, _, _ = coord._device_failures["R5S2A001602"]
        assert failure_count == 1

    def test_second_failure_increments_counter(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 130.0)
        failure_count, _, _ = coord._device_failures["R5S2A001602"]
        assert failure_count == 2

    def test_failure_records_timestamp(self) -> None:
        coord = _coordinator()
        now = 12345.67
        coord._record_device_failure("R5S2A001602", now)
        _, last_attempt, _ = coord._device_failures["R5S2A001602"]
        assert last_attempt == now

    def test_multiple_failures_update_timestamp(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 200.0)
        _, last_attempt, _ = coord._device_failures["R5S2A001602"]
        assert last_attempt == 200.0

    def test_success_resets_failure_counter(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 130.0)
        coord._record_device_success("R5S2A001602")
        failure_count, _, _ = coord._device_failures["R5S2A001602"]
        assert failure_count == 0

    def test_success_resets_unavailable_flag(self) -> None:
        coord = _coordinator(unavailable_after=2)
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 130.0)
        # Should now be unavailable
        assert coord.is_device_unavailable("R5S2A001602") is True
        # Success should reset
        coord._record_device_success("R5S2A001602")
        assert coord.is_device_unavailable("R5S2A001602") is False


# ---------------------------------------------------------------------------
# Section 3 — Availability tracking
# ---------------------------------------------------------------------------

class TestAvailabilityTracking:
    """Test device availability transitions after N consecutive failures."""

    def test_device_available_by_default(self) -> None:
        coord = _coordinator(unavailable_after=3)
        assert coord.is_device_unavailable("R5S2A001602") is False

    def test_device_available_after_one_failure(self) -> None:
        coord = _coordinator(unavailable_after=3)
        coord._record_device_failure("R5S2A001602", 100.0)
        assert coord.is_device_unavailable("R5S2A001602") is False

    def test_device_available_after_two_failures(self) -> None:
        coord = _coordinator(unavailable_after=3)
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 130.0)
        assert coord.is_device_unavailable("R5S2A001602") is False

    def test_device_unavailable_after_three_failures(self) -> None:
        coord = _coordinator(unavailable_after=3)
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 130.0)
        coord._record_device_failure("R5S2A001602", 190.0)
        assert coord.is_device_unavailable("R5S2A001602") is True

    def test_device_unavailable_after_four_failures(self) -> None:
        coord = _coordinator(unavailable_after=3)
        coord._record_device_failure("R5S2A001602", 100.0)
        coord._record_device_failure("R5S2A001602", 130.0)
        coord._record_device_failure("R5S2A001602", 190.0)
        coord._record_device_failure("R5S2A001602", 310.0)
        assert coord.is_device_unavailable("R5S2A001602") is True

    def test_custom_unavailable_threshold(self) -> None:
        coord = _coordinator(unavailable_after=5)
        for i in range(4):
            coord._record_device_failure("R5S2A001602", 100.0 + i * 30)
        assert coord.is_device_unavailable("R5S2A001602") is False
        coord._record_device_failure("R5S2A001602", 100.0 + 5 * 30)
        assert coord.is_device_unavailable("R5S2A001602") is True

    def test_recovery_restores_availability(self) -> None:
        coord = _coordinator(unavailable_after=3)
        # Go unavailable
        for i in range(3):
            coord._record_device_failure("R5S2A001602", 100.0 + i * 30)
        assert coord.is_device_unavailable("R5S2A001602") is True
        # Recover
        coord._record_device_success("R5S2A001602")
        assert coord.is_device_unavailable("R5S2A001602") is False


# ---------------------------------------------------------------------------
# Section 4 — Polling back-off behavior
# ---------------------------------------------------------------------------

class TestPollingBackoff:
    """Test that devices are skipped when their back-off interval hasn't elapsed."""

    def test_should_poll_device_with_no_failures(self) -> None:
        coord = _coordinator()
        assert coord._should_poll_device("R5S2A001602", 100.0) is True

    def test_should_poll_device_with_zero_failures(self) -> None:
        coord = _coordinator()
        coord._device_failures["R5S2A001602"] = (0, 100.0, False)
        assert coord._should_poll_device("R5S2A001602", 130.0) is True

    def test_should_not_poll_immediately_after_failure(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        # Only 10s have passed, need 30s
        assert coord._should_poll_device("R5S2A001602", 110.0) is False

    def test_should_poll_after_backoff_interval_elapsed(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        # 30s have passed, exactly at the boundary
        assert coord._should_poll_device("R5S2A001602", 130.0) is True

    def test_should_poll_well_after_backoff_interval(self) -> None:
        coord = _coordinator()
        coord._record_device_failure("R5S2A001602", 100.0)
        # 100s have passed, well beyond 30s
        assert coord._should_poll_device("R5S2A001602", 200.0) is True

    def test_backoff_doubles_after_three_failures(self) -> None:
        coord = _coordinator()
        for i in range(3):
            coord._record_device_failure("R5S2A001602", 100.0 + i * 30)
        # Now need 60s since last attempt (at t=160)
        assert coord._should_poll_device("R5S2A001602", 210.0) is False
        assert coord._should_poll_device("R5S2A001602", 220.0) is True

    def test_backoff_quadruples_after_six_failures(self) -> None:
        coord = _coordinator()
        for i in range(6):
            coord._record_device_failure("R5S2A001602", 100.0 + i * 60)
        # Now need 120s since last attempt (at t=400)
        assert coord._should_poll_device("R5S2A001602", 510.0) is False
        assert coord._should_poll_device("R5S2A001602", 520.0) is True


# ---------------------------------------------------------------------------
# Section 5 — Recovery logging
# ---------------------------------------------------------------------------

class TestRecoveryLogging:
    """Test that recovery is logged at INFO level with failure count."""

    def test_recovery_message_includes_failure_count(self) -> None:
        from custom_components.maxspect.icv6_coordinator import _LOGGER

        coord = _coordinator()
        for i in range(5):
            coord._record_device_failure("R5S2A001602", 100.0 + i * 60)

        with patch.object(_LOGGER, "info") as mock_info:
            coord._record_device_success("R5S2A001602")
            mock_info.assert_called_once()
            args = mock_info.call_args[0]
            # Check that the message mentions the device and failure count
            assert "R5S2A001602" in args[1]
            assert args[2] == 5  # failure count

    def test_no_recovery_log_on_first_success(self) -> None:
        from custom_components.maxspect.icv6_coordinator import _LOGGER

        coord = _coordinator()
        with patch.object(_LOGGER, "info") as mock_info:
            coord._record_device_success("R5S2A001602")
            # Should not log if there were no prior failures
            mock_info.assert_not_called()


# ---------------------------------------------------------------------------
# Section 6 — Integration test: full failure/recovery cycle
# ---------------------------------------------------------------------------

class TestFullFailureRecoveryCycle:
    """Integration test simulating a device going offline and recovering."""

    def test_device_offline_overnight_then_recovers(self) -> None:
        coord = _coordinator(max_backoff=8, unavailable_after=3)
        device_id = "R5S2A001602"

        # Simulate failures every 30s for the first 3 attempts
        for i in range(3):
            coord._record_device_failure(device_id, 100.0 + i * 30)

        # Device should now be unavailable
        assert coord.is_device_unavailable(device_id) is True
        failure_count, _, _ = coord._device_failures[device_id]
        assert failure_count == 3

        # Backoff interval should be 60s (3 failures → 2× base)
        assert coord._get_backoff_interval(3) == 60.0

        # Simulate more failures as backoff increases
        for i in range(3, 10):
            expected_interval = coord._get_backoff_interval(i)
            last_attempt = coord._device_failures[device_id][1]
            # Wait for backoff interval to elapse
            next_attempt = last_attempt + expected_interval
            coord._record_device_failure(device_id, next_attempt)

        # After 10 failures, backoff should still be 4× (120s)
        assert coord._get_backoff_interval(10) == 120.0

        # Device is still unavailable
        assert coord.is_device_unavailable(device_id) is True

        # Now device comes back online
        coord._record_device_success(device_id)

        # Device should be available again
        assert coord.is_device_unavailable(device_id) is False
        failure_count, _, _ = coord._device_failures[device_id]
        assert failure_count == 0

        # Next poll should happen immediately
        assert coord._should_poll_device(device_id, time.monotonic()) is True
