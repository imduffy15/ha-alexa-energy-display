"""Alexa Smart Home directives through the authenticated Home Assistant view."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_energy_display import snapshot as snap
from custom_components.alexa_energy_display.const import DOMAIN, LWA_TOKEN_URL, SMART_HOME_PATH
from custom_components.alexa_energy_display.snapshot import EnergySources

EU = "https://api.eu.amazonalexa.com"
SOURCES = EnergySources(
    solar_power=["sensor.solar_w"],
    grid_power=["sensor.grid_w"],
    battery_power=["sensor.batt_w"],
    battery_soc=["sensor.batt_soc"],
    solar_energy=["sensor.solar_kwh"],
    grid_import=["sensor.import_kwh"],
)
TODAY = {"sensor.solar_kwh": 8.456, "sensor.import_kwh": 3.0}


@pytest.fixture
def energy():
    async def sources(_hass):
        return SOURCES

    async def today(_hass, ids):
        values = [TODAY[i] for i in ids if i in TODAY]
        return sum(values) if values else None

    with patch("custom_components.alexa_energy_display.coordinator.async_get_energy_sources", sources), patch.object(
        snap, "async_energy_today", today
    ):
        yield


def set_states(hass, soc="76.4", batt="-1500"):
    hass.states.async_set("sensor.solar_w", "2100", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_w", "-0.6", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.batt_w", batt, {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.batt_soc", soc, {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.hot_water", "48.25", {"friendly_name": "Hot water", "unit_of_measurement": "°C"})
    hass.states.async_set("sensor.car", "81", {"friendly_name": "Car charge", "unit_of_measurement": "%"})
    hass.states.async_set("sensor.tariff", "0.2312", {"friendly_name": "Tariff", "unit_of_measurement": "€/kWh"})
    hass.states.async_set("select.mode", "self_use", {"friendly_name": "Inverter mode"})


async def setup(hass, **options):
    hass.config.external_url = "https://ha.example.com"
    set_states(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Energy",
        data={"webhook_id": "b" * 64},
        options={
            "title": "Energy",
            "use_energy_dashboard": True,
            "entities": ["sensor.hot_water", "sensor.car", "sensor.tariff", "select.mode"],
            "update_interval": 60,
            **options,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def directive(namespace, name, endpoint_id=None, payload=None):
    d = {"header": {"namespace": namespace, "name": name, "payloadVersion": "3", "messageId": "m1",
                    "correlationToken": "c1"}, "payload": payload or {}}
    if endpoint_id:
        d["endpoint"] = {"endpointId": endpoint_id, "scope": {"type": "BearerToken", "token": "t"}}
    return {"directive": d}


async def post(client, body):
    resp = await client.post(SMART_HOME_PATH, json=body)
    assert resp.status == 200
    return await resp.json()


async def test_requires_home_assistant_auth(hass, hass_client_no_auth, energy):
    await setup(hass)
    client = await hass_client_no_auth()
    resp = await client.post(SMART_HOME_PATH, json=directive("Alexa.Discovery", "Discover"))
    assert resp.status == 401


async def test_discovery(hass, hass_client, energy):
    entry = await setup(hass, voice_prefix="Ballyer")
    client = await hass_client()
    out = await post(client, directive("Alexa.Discovery", "Discover", payload={"scope": {"type": "BearerToken", "token": "t"}}))
    assert out["event"]["header"]["name"] == "Discover.Response"
    endpoints = {e["friendlyName"]: e for e in out["event"]["payload"]["endpoints"]}
    assert set(endpoints) == {
        "Ballyer Solar", "Ballyer Grid", "Ballyer Battery", "Ballyer Home Energy",
        "Ballyer Hot water", "Ballyer Car charge", "Ballyer Tariff",
    }  # the text-only select is skipped
    battery = endpoints["Ballyer Battery"]
    assert battery["endpointId"] == f"{entry.entry_id}-battery"
    caps = {(c["interface"], c.get("instance")): c for c in battery["capabilities"]}
    level = caps[("Alexa.RangeController", "Battery.Level")]
    assert level["properties"]["nonControllable"] is True
    assert level["properties"]["proactivelyReported"] is True
    assert level["configuration"]["unitOfMeasure"] == "Alexa.Unit.Percent"
    state = caps[("Alexa.ModeController", "Battery.State")]
    assert [m["value"] for m in state["configuration"]["supportedModes"]] == [
        "Battery.State.Charging", "Battery.State.Discharging", "Battery.State.Idle"
    ]
    assert ("Alexa", None) in caps and ("Alexa.EndpointHealth", None) in caps
    assert endpoints["Ballyer Hot water"]["displayCategories"] == ["TEMPERATURE_SENSOR"]
    names = {n["value"]["text"] for n in level["capabilityResources"]["friendlyNames"]}
    assert "Level" in names
    assert {n["value"]["locale"] for n in level["capabilityResources"]["friendlyNames"]} >= {"en-GB", "en-US"}


async def test_report_state(hass, hass_client, energy):
    entry = await setup(hass)
    client = await hass_client()

    async def props(suffix):
        out = await post(client, directive("Alexa", "ReportState", f"{entry.entry_id}-{suffix}"))
        assert out["event"]["header"]["name"] == "StateReport"
        assert out["event"]["header"]["correlationToken"] == "c1"
        return {(p["namespace"], p.get("instance"), p["name"]): p["value"] for p in out["context"]["properties"]}

    battery = await props("battery")
    assert battery[("Alexa.RangeController", "Battery.Level", "rangeValue")] == 76
    assert battery[("Alexa.RangeController", "Battery.Power", "rangeValue")] == -1500
    assert battery[("Alexa.ModeController", "Battery.State", "mode")] == "Battery.State.Charging"
    assert battery[("Alexa.EndpointHealth", None, "connectivity")] == {"value": "OK"}
    grid = await props("grid")
    assert grid[("Alexa.RangeController", "Grid.Power", "rangeValue")] == -600
    assert grid[("Alexa.ModeController", "Grid.Direction", "mode")] == "Grid.Direction.Exporting"
    assert grid[("Alexa.RangeController", "Grid.ImportToday", "rangeValue")] == 3.0
    solar = await props("solar")
    assert solar[("Alexa.RangeController", "Solar.Power", "rangeValue")] == 2100
    assert solar[("Alexa.RangeController", "Solar.Today", "rangeValue")] == 8.46
    home = await props("home")
    assert home[("Alexa.RangeController", "Home.Power", "rangeValue")] == 0  # 2.1 - 0.6 - 1.5
    water = await props("entity-sensor_hot_water")
    assert water[("Alexa.TemperatureSensor", None, "temperature")] == {"value": 48.2, "scale": "CELSIUS"}
    car = await props("entity-sensor_car")
    assert car[("Alexa.RangeController", "Entity.Value", "rangeValue")] == 81
    tariff = await props("entity-sensor_tariff")
    assert tariff[("Alexa.RangeController", "Entity.Value", "rangeValue")] == 0.23


async def test_errors(hass, hass_client, energy):
    entry = await setup(hass)
    client = await hass_client()
    out = await post(client, directive("Alexa", "ReportState", "nope"))
    assert out["event"]["payload"]["type"] == "NO_SUCH_ENDPOINT"
    out = await post(client, directive("Alexa.RangeController", "SetRangeValue", f"{entry.entry_id}-battery",
                                       {"rangeValue": 10}))
    assert out["event"]["payload"]["type"] == "NOT_SUPPORTED_IN_CURRENT_MODE"
    bad = directive("Alexa.Discovery", "Discover")
    bad["directive"]["header"]["payloadVersion"] = "2"
    assert (await post(client, bad))["event"]["payload"]["type"] == "INVALID_DIRECTIVE"


async def test_accept_grant_and_change_reports(hass, hass_client, energy, aioclient_mock):
    aioclient_mock.post(LWA_TOKEN_URL, json={"access_token": "Atza|ev", "refresh_token": "Atzr|r", "expires_in": 3600})
    aioclient_mock.post(f"{EU}/v3/events", status=202)
    entry = await setup(hass, smart_home_client_id="shc", smart_home_client_secret="shs")
    entry.runtime_data.users["amzn1.ask.account.X"] = {"api_endpoint": EU, "devices": []}
    client = await hass_client()
    out = await post(client, directive("Alexa.Authorization", "AcceptGrant", payload={
        "grant": {"type": "OAuth2.AuthorizationCode", "code": "ANCODE"},
        "grantee": {"type": "BearerToken", "token": "t"}}))
    assert out["event"]["header"]["name"] == "AcceptGrant.Response"
    token_call = aioclient_mock.mock_calls[-1]
    assert token_call[2]["grant_type"] == "authorization_code" and token_call[2]["code"] == "ANCODE"
    assert token_call[2]["client_id"] == "shc"

    coordinator = entry.runtime_data
    await coordinator.async_refresh()  # baseline, nothing reported
    await hass.async_block_till_done()
    events = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v3/events")]
    assert events == []

    set_states(hass, soc="80", batt="900")  # level 76 -> 80, charging -> discharging
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    events = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/v3/events")]
    reported = {(e[2]["event"]["endpoint"]["endpointId"].split("-", 1)[1],
                 e[2]["event"]["payload"]["change"]["properties"][0]["instance"]): e[2]["event"]["payload"]["change"]["properties"][0]["value"]
                for e in events}
    assert reported == {("battery", "Battery.Level"): 80, ("battery", "Battery.State"): "Battery.State.Discharging"}
    assert events[0][3]["Authorization"] == "Bearer Atza|ev"


async def test_accept_grant_without_credentials(hass, hass_client, energy):
    await setup(hass)
    client = await hass_client()
    out = await post(client, directive("Alexa.Authorization", "AcceptGrant", payload={
        "grant": {"type": "OAuth2.AuthorizationCode", "code": "X"}, "grantee": {"type": "BearerToken", "token": "t"}}))
    assert out["event"]["header"]["name"] == "AcceptGrant.Response"


async def test_devices_survive_unavailable_sensors(hass, hass_client, energy):
    """After a restart, sensors can be unavailable for a while; devices must not vanish."""
    entry = await setup(hass)
    for entity_id in ("sensor.solar_w", "sensor.grid_w", "sensor.batt_w", "sensor.batt_soc"):
        hass.states.async_set(entity_id, "unavailable")
    await entry.runtime_data.async_refresh()
    client = await hass_client()
    out = await post(client, directive("Alexa.Discovery", "Discover"))
    names = {e["friendlyName"]: e for e in out["event"]["payload"]["endpoints"]}
    assert {"Solar", "Grid", "Battery", "Home Energy"} <= set(names)
    instances = {c.get("instance") for c in names["Battery"]["capabilities"]}
    assert {"Battery.Level", "Battery.Power", "Battery.State"} <= instances
    state = await post(client, directive("Alexa", "ReportState", f"{entry.entry_id}-battery"))
    props = state["context"]["properties"]
    assert props == [p for p in props if p["namespace"] == "Alexa.EndpointHealth"]
    assert props[0]["value"] == {"value": "UNREACHABLE"}
