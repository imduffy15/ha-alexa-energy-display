"""Energy preference parsing, formatting and tile building."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from custom_components.alexa_energy_display import snapshot as snap
from custom_components.alexa_energy_display.snapshot import (
    assemble,
    async_energy_tiles,
    entity_tiles,
    format_energy,
    format_power,
    parse_energy_prefs,
)

UNIFIED = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.solar_kwh", "stat_rate": "sensor.solar_w"},
        {
            "type": "grid",
            "stat_energy_from": "sensor.import_kwh",
            "stat_energy_to": "sensor.export_kwh",
            "stat_rate": "sensor.grid_w",
            "cost_adjustment_day": 0,
        },
        {
            "type": "battery",
            "stat_energy_from": "sensor.batt_out_kwh",
            "stat_energy_to": "sensor.batt_in_kwh",
            "stat_rate": "sensor.batt_w",
            "stat_soc": "sensor.batt_soc",
        },
        {"type": "gas", "stat_energy_from": "sensor.gas"},
    ],
    "device_consumption": [],
}
LEGACY = {
    "energy_sources": [
        {
            "type": "grid",
            "flow_from": [{"stat_energy_from": "sensor.t1"}, {"stat_energy_from": "sensor.t2"}],
            "flow_to": [{"stat_energy_to": "sensor.ret"}],
            "power": [{"stat_rate": "sensor.grid_kw"}],
            "cost_adjustment_day": 0,
        }
    ],
    "device_consumption": [],
}


def test_parse_unified_and_legacy():
    u = parse_energy_prefs(UNIFIED)
    assert u.solar_energy == ["sensor.solar_kwh"] and u.solar_power == ["sensor.solar_w"]
    assert u.grid_import == ["sensor.import_kwh"] and u.grid_export == ["sensor.export_kwh"]
    assert u.battery_out == ["sensor.batt_out_kwh"] and u.battery_in == ["sensor.batt_in_kwh"]
    assert u.battery_soc == ["sensor.batt_soc"]
    legacy = parse_energy_prefs(LEGACY)
    assert legacy.grid_import == ["sensor.t1", "sensor.t2"]
    assert legacy.grid_export == ["sensor.ret"] and legacy.grid_power == ["sensor.grid_kw"]
    assert parse_energy_prefs(None).empty


@pytest.mark.parametrize(
    ("kw", "expected"),
    [(0.85, ("850", "W")), (-0.0001, ("0", "W")), (2.345, ("2.35", "kW")), (12.34, ("12.3", "kW"))],
)
def test_format_power(kw, expected):
    assert format_power(kw) == expected


def test_format_energy():
    assert format_energy(3.456) == ("3.46", "kWh")
    assert format_energy(123.45) == ("123.5", "kWh")
    assert format_energy(2500) == ("2.50", "MWh")


async def test_energy_tiles_live_and_today(hass):
    hass.states.async_set("sensor.solar_w", "2100", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_w", "-0.6", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.batt_w", "-1500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.batt_soc", "76.4", {"unit_of_measurement": "%"})
    today = {
        "sensor.solar_kwh": 8.4,
        "sensor.import_kwh": 3.0,
        "sensor.export_kwh": 1.2,
        "sensor.batt_in_kwh": 2.0,
        "sensor.batt_out_kwh": 1.5,
    }

    async def fake_today(_hass, ids):
        values = [today[i] for i in ids if i in today]
        return sum(values) if values else None

    with patch.object(snap, "async_energy_today", fake_today):
        tiles = await async_energy_tiles(hass, parse_energy_prefs(UNIFIED))
    by_id = {t["id"]: t for t in tiles}
    assert [t["id"] for t in tiles][:4] == ["solar_now", "grid_now", "battery", "home_now"]
    assert by_id["solar_now"]["value"] == "2.10" and by_id["solar_now"]["unit"] == "kW"
    assert by_id["grid_now"]["label"] == "Grid export" and by_id["grid_now"]["value"] == "600"
    assert by_id["battery"]["value"] == "76" and by_id["battery"]["percent"] == 76.4
    assert by_id["battery"]["sub"] == "charging 1.50 kW"
    # home = solar 2.1 + grid -0.6 + battery -1.5 = 0 kW
    assert by_id["home_now"]["value"] == "0" and by_id["home_now"]["unit"] == "W"
    # home today = 8.4 + 3.0 - 1.2 + 1.5 - 2.0 = 9.7
    assert by_id["home_today"]["value"] == "9.70"
    assert by_id["battery_today"]["value"] == "2.00/1.50"


async def test_unavailable_sources_are_skipped(hass):
    hass.states.async_set("sensor.solar_w", "unavailable", {"unit_of_measurement": "W"})

    async def nothing(_hass, _ids):
        return None

    with patch.object(snap, "async_energy_tiles", wraps=async_energy_tiles), patch.object(
        snap, "async_energy_today", nothing
    ):
        assert await async_energy_tiles(hass, parse_energy_prefs(UNIFIED)) == []


async def test_entity_tiles(hass):
    hass.states.async_set(
        "sensor.tariff", "0.2312", {"friendly_name": "Tariff", "unit_of_measurement": "€/kWh", "device_class": "monetary"}
    )
    hass.states.async_set("sensor.car", "81", {"friendly_name": "Car", "unit_of_measurement": "%", "device_class": "battery"})
    hass.states.async_set("select.mode", "self_use", {"friendly_name": "Inverter mode"})
    hass.states.async_set("sensor.gone", "unavailable", {"friendly_name": "Gone", "unit_of_measurement": "W"})
    tiles = entity_tiles(hass, ["sensor.tariff", "sensor.car", "select.mode", "sensor.gone", "sensor.missing"])
    assert [(t["label"], t["value"], t["unit"]) for t in tiles] == [
        ("Tariff", "0.23", "€/kWh"),
        ("Car", "81.0", "%"),
        ("Inverter mode", "self use", ""),
        ("Gone", "—", ""),
    ]
    assert tiles[1]["percent"] == 81.0


def test_assemble_limits_and_speech():
    tiles = [snap.tile(f"t{i}", f"T{i}", str(i), "kW", sub="now") for i in range(15)]
    out = assemble("Energy", tiles, datetime(2026, 11, 15, 14, 5))
    assert len(out["tiles"]) == 12 and len(out["widget"]) == 4
    assert out["status"] == "ok" and out["v"] == 1
    assert out["speech"].startswith("T0 now: 0 kilowatts.")
    assert assemble("Energy", [])["status"] == "empty"


def test_snapshot_fits_datastore_limit():
    """16 KB is the Data Store commands payload limit."""
    import json

    tiles = [snap.tile(f"sensor.{'x' * 60}{i}", "L" * 40, "1" * 20, "u" * 10, sub="s" * 40, percent=50) for i in range(30)]
    assert len(json.dumps(assemble("T" * 60, tiles))) < 8000
