"""Tests for marking devices unavailable after a factor of their interval."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from habluetooth import (
    BluetoothServiceInfoBleak,
    HaBluetoothConnector,
    get_manager,
)

from . import (
    InjectableRemoteScanner,
    MockBleakClient,
    generate_advertisement_data,
    generate_ble_device,
    patch_bluetooth_time,
)

ADDRESS = "44:44:33:11:23:45"
LEARNED_INTERVAL = 20.0
FACTOR = 3.0


def _scanner(source: str) -> InjectableRemoteScanner:
    connector = HaBluetoothConnector(
        MockBleakClient, "mock_bleak_client", lambda: False
    )
    return InjectableRemoteScanner(source, source, connector, False)


def _advertise(scanner: InjectableRemoteScanner, now: float, rssi: int = -60) -> None:
    device = generate_ble_device(ADDRESS, "sensor", {}, rssi=rssi)
    adv = generate_advertisement_data(
        local_name="sensor",
        service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": b"\x40\x00"},
        rssi=rssi,
    )
    with patch_bluetooth_time(now):
        scanner.inject_advertisement(device, adv, now)


def _check(now: float) -> None:
    with patch_bluetooth_time(now):
        get_manager()._async_check_fast_unavailable()


@pytest_asyncio.fixture
async def setup(enable_bluetooth):
    """Two scanners, a tracked device and an unavailable list."""
    manager = get_manager()
    scanners = [_scanner("esp32_a"), _scanner("esp32_b")]
    unsetups = [scanner.async_setup() for scanner in scanners]
    cancels = [manager.async_register_scanner(scanner) for scanner in scanners]
    unavailable: list[BluetoothServiceInfoBleak] = []
    untrack = manager.async_track_unavailable(
        unavailable.append, ADDRESS, connectable=False
    )
    yield manager, scanners, unavailable
    manager.async_set_unavailable_factor(ADDRESS, None)
    untrack()
    for cancel in cancels:
        cancel()
    for unsetup in unsetups:
        unsetup()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_marked_unavailable_after_factor_times_interval(setup) -> None:
    """No scanner hearing it for factor x interval marks it unavailable."""
    manager, (scanner_a, _), unavailable = setup
    manager.async_set_unavailable_factor(ADDRESS, FACTOR)
    start = time.monotonic()
    _advertise(scanner_a, start)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL

    _check(start + FACTOR * LEARNED_INTERVAL)
    assert unavailable == []

    _check(start + FACTOR * LEARNED_INTERVAL + 1)
    assert len(unavailable) == 1
    assert ADDRESS not in manager._all_history
    assert ADDRESS not in manager._advertisement_tracker.intervals

    # Once marked, it is not marked again.
    _check(start + 10 * FACTOR * LEARNED_INTERVAL)
    assert len(unavailable) == 1


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_any_scanner_keeps_it_available(setup) -> None:
    """A device the owner no longer hears stays available if another does."""
    manager, (scanner_a, scanner_b), unavailable = setup
    manager.async_set_unavailable_factor(ADDRESS, FACTOR)
    start = time.monotonic()
    _advertise(scanner_a, start, rssi=-50)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL
    # The weaker scanner keeps hearing it; the owner does not.
    later = start + 2 * LEARNED_INTERVAL
    _advertise(scanner_b, later, rssi=-90)
    assert manager._all_history[ADDRESS].source == "esp32_a"

    _check(start + FACTOR * LEARNED_INTERVAL + 1)
    assert unavailable == []

    _check(later + FACTOR * LEARNED_INTERVAL + 1)
    assert len(unavailable) == 1


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_no_factor_or_no_interval_waits_for_the_sweep(setup) -> None:
    """Without a factor, or without a learned interval, nothing changes."""
    manager, (scanner_a, _), unavailable = setup
    start = time.monotonic()
    _advertise(scanner_a, start)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL
    _check(start + 10 * FACTOR * LEARNED_INTERVAL)
    assert unavailable == []

    manager.async_set_unavailable_factor(ADDRESS, FACTOR)
    manager._advertisement_tracker.intervals.pop(ADDRESS)
    _advertise(scanner_a, start + 1)
    _check(start + 10 * FACTOR * LEARNED_INTERVAL)
    assert unavailable == []
    assert ADDRESS in manager._all_history


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_available_again_on_next_advertisement(setup) -> None:
    """After being marked, the next advertisement brings it back."""
    manager, (scanner_a, _), unavailable = setup
    manager.async_set_unavailable_factor(ADDRESS, FACTOR)
    start = time.monotonic()
    _advertise(scanner_a, start)
    manager._advertisement_tracker.intervals[ADDRESS] = LEARNED_INTERVAL
    _check(start + FACTOR * LEARNED_INTERVAL + 1)
    assert len(unavailable) == 1

    _advertise(scanner_a, start + FACTOR * LEARNED_INTERVAL + 2)
    assert ADDRESS in manager._all_history
    assert manager.async_get_unavailable_factor(ADDRESS) == FACTOR


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_set_and_clear_factor() -> None:
    """The factor is validated, read back and cleared with None."""
    manager = get_manager()
    for bad in (1.0, 0.5, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="factor must be"):
            manager.async_set_unavailable_factor(ADDRESS, bad)
    manager.async_set_unavailable_factor(ADDRESS, FACTOR)
    assert manager.async_get_unavailable_factor(ADDRESS) == FACTOR
    manager.async_set_unavailable_factor(ADDRESS, None)
    assert manager.async_get_unavailable_factor(ADDRESS) is None
    assert ADDRESS not in manager._last_heard
