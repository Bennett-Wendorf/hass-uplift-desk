"""Tests for height notification handling through the real DeskController.

A height notification pushed through the fake client's notify handler must
flow through the production ``DeskController`` notification processor and
update ``coordinator.height_mm`` (and therefore the sensor state).
"""

from __future__ import annotations

from custom_components.uplift_desk.sensor import DeskHeightSensor

from .conftest import make_notification_packet, wait_until


def make_units_packet(unit_byte: int) -> bytes:
    """Opcode 0x0E (display unit preference): 1 byte (0x00=cm, 0x01=in)."""
    return make_notification_packet(0x0E, bytes([unit_byte]))


def make_height_packet(height_tenths: int) -> bytes:
    """Opcode 0x01 (current height): 2-byte BE height in tenths + 1 unknown byte."""
    return make_notification_packet(0x01, height_tenths.to_bytes(2, "big") + b"\x00")


async def test_height_notification_updates_coordinator_and_sensor(
    fake_ble, coordinator
):
    """A 0x01 height notification updates coordinator.height_mm and the sensor."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()
    sensor = DeskHeightSensor(coordinator)

    # The desk reports its display unit (cm), then the current height (75.0 cm).
    await client.simulate_notification(make_units_packet(0x00))
    await client.simulate_notification(make_height_packet(750))

    await wait_until(lambda: coordinator.height_mm == 750.0)
    assert coordinator.height_mm == 750.0
    assert sensor.native_value == 750.0
    assert sensor.available is True


async def test_height_notification_in_inches_is_converted(fake_ble, coordinator):
    """Height is converted from the desk's display unit to millimeters."""
    client = fake_ble.valid_client()
    fake_ble.queue_client(client)
    await coordinator.async_connect()

    # 30.0 inches == 762 mm.
    await client.simulate_notification(make_units_packet(0x01))
    await client.simulate_notification(make_height_packet(300))

    await wait_until(lambda: coordinator.height_mm == 762.0)
    assert coordinator.height_mm == 762.0
