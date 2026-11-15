"""Today's totals come from real recorder statistics."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.alexa_energy_display.snapshot import async_energy_today


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder must start before hass, so override the shared fixture."""
    yield


async def test_change_since_midnight(hass):
    midnight = dt_util.start_of_local_day()
    metadata = {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": "Solar",
        "source": "test",
        "statistic_id": "test:solar_energy",
        "unit_of_measurement": "kWh",
        "unit_class": "energy",
    }
    rows = [
        {"start": midnight - timedelta(hours=2), "state": 100.0, "sum": 100.0},
        {"start": midnight - timedelta(hours=1), "state": 101.0, "sum": 101.0},
        {"start": midnight, "state": 103.0, "sum": 103.5},
        {"start": midnight + timedelta(hours=1), "state": 106.0, "sum": 106.0},
    ]
    async_add_external_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)
    # sum at end of last hour (106.0) minus sum before midnight (101.0)
    assert await async_energy_today(hass, ["test:solar_energy"]) == 5.0
    assert await async_energy_today(hass, ["test:missing"]) is None
