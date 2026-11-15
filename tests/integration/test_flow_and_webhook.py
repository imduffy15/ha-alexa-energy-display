"""Config flow, setup, webhook pairing and Data Store push."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_energy_display.const import DOMAIN, LWA_TOKEN_URL

EU = "https://api.eu.amazonalexa.com"
USER = "amzn1.ask.account.TESTUSER"
DEVICE = "amzn1.ask.device.TESTDEVICE"
WEBHOOK_ID = "a" * 64


@pytest.fixture
def lwa(aioclient_mock):
    aioclient_mock.post(LWA_TOKEN_URL, json={"access_token": "Atc|token", "expires_in": 3600, "token_type": "bearer"})
    aioclient_mock.post(
        f"{EU}/v1/datastore/commands",
        json={"results": [{"deviceId": DEVICE, "type": "SUCCESS"}]},
    )
    return aioclient_mock


@pytest.fixture
def no_energy():
    from custom_components.alexa_energy_display.snapshot import EnergySources

    async def empty(_hass):
        return EnergySources()

    with patch("custom_components.alexa_energy_display.coordinator.async_get_energy_sources", empty):
        yield


async def _setup(hass: HomeAssistant, **options) -> MockConfigEntry:
    hass.config.external_url = "https://ha.example.com"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Energy",
        data={"webhook_id": WEBHOOK_ID},
        options={
            "title": "Energy",
            "use_energy_dashboard": False,
            "entities": ["sensor.solar_power"],
            "update_interval": 60,
            **options,
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.solar_power", "2.5", {"friendly_name": "Solar power", "unit_of_measurement": "kW"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_validates_and_creates(hass, lwa, no_energy):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"title": "House", "client_id": "amzn1.application-oa2-client.x"}
    )
    assert result["errors"] == {"base": "incomplete_credentials"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"title": "House", "client_id": "amzn1.application-oa2-client.x", "client_secret": "s3cret"},
    )
    assert result["step_id"] == "sources"
    token_call = lwa.mock_calls[-1]
    assert token_call[2]["scope"] == "alexa::datastore"
    assert token_call[2]["grant_type"] == "client_credentials"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"use_energy_dashboard": False, "entities": [], "entities_first": False, "update_interval": 60}
    )
    assert result["errors"] == {"base": "no_sources"}

    hass.config.external_url = "https://ha.example.com"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"use_energy_dashboard": True, "entities": [], "entities_first": False, "update_interval": 90.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "House"
    assert len(result["data"]["webhook_id"]) == 64
    assert result["options"]["update_interval"] == 90
    assert result["options"]["client_secret"] == "s3cret"
    await hass.async_block_till_done()


async def test_config_flow_rejected_credentials(hass, aioclient_mock):
    aioclient_mock.post(LWA_TOKEN_URL, status=401, json={"error": "invalid_client"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"title": "House", "client_id": "id", "client_secret": "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_pull_only_webhook(hass, hass_client_no_auth, no_energy):
    with patch("custom_components.alexa_energy_display.persistent_notification.async_create") as notify:
        entry = await _setup(hass)
    assert f"https://ha.example.com/api/webhook/{WEBHOOK_ID}" in notify.call_args[0][1]
    client = await hass_client_no_auth()

    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json={"type": "launch", "user_id": USER})
    assert resp.status == 200
    body = await resp.json()
    assert body["widget"] is False
    assert body["snapshot"]["tiles"][0] == {
        "id": "sensor.solar_power",
        "label": "Solar power",
        "value": "2.50",
        "unit": "kW",
        "color": "#B48CFF",
    }
    assert hass.states.get("sensor.energy_widget_status").state == "pull_only"

    assert (await client.post(f"/api/webhook/{WEBHOOK_ID}", data="nope")).status == 400
    assert (await client.post(f"/api/webhook/{WEBHOOK_ID}", json={"type": "bogus"})).status == 400
    assert (await client.get(f"/api/webhook/{WEBHOOK_ID}")).status == 405
    await hass.config_entries.async_unload(entry.entry_id)


async def test_pairing_pushes_to_datastore(hass, hass_client_no_auth, lwa, no_energy):
    entry = await _setup(hass, client_id="cid", client_secret="secret")
    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        json={"type": "launch", "user_id": USER, "device_id": DEVICE, "api_endpoint": EU, "skill_id": "amzn1.ask.skill.x"},
    )
    assert resp.status == 200
    assert (await resp.json())["widget"] is True
    await hass.async_block_till_done()

    pushes = [c for c in lwa.mock_calls if str(c[1]).endswith("/v1/datastore/commands")]
    assert len(pushes) == 1
    _method, _url, body, headers = pushes[0]
    assert headers["Authorization"] == "Bearer Atc|token"
    assert body["target"] == {"type": "USER", "id": USER}
    assert [c["type"] for c in body["commands"]] == ["PUT_NAMESPACE", "PUT_OBJECT"]
    put = body["commands"][1]
    assert (put["namespace"], put["key"]) == ("haEnergy", "snapshot")
    assert put["content"]["widget"][0]["value"] == "2.50"
    assert body["attemptDeliveryUntil"].endswith("Z")

    state = hass.states.get("sensor.energy_widget_status")
    assert state.state == "paired"
    assert state.attributes["paired_accounts"] == 1
    assert state.attributes["last_push_result"] == "1 delivered"

    # Unchanged data is not pushed again on the next interval.
    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    pushes = [c for c in lwa.mock_calls if str(c[1]).endswith("/v1/datastore/commands")]
    assert len(pushes) == 1
    # Changed data is.
    hass.states.async_set("sensor.solar_power", "3.1", {"friendly_name": "Solar power", "unit_of_measurement": "kW"})
    await coordinator.async_refresh()
    pushes = [c for c in lwa.mock_calls if str(c[1]).endswith("/v1/datastore/commands")]
    assert len(pushes) == 2

    # The button forces a push.
    await hass.services.async_call("button", "press", {"entity_id": "button.energy_push_to_widget_now"}, blocking=True)
    pushes = [c for c in lwa.mock_calls if str(c[1]).endswith("/v1/datastore/commands")]
    assert len(pushes) == 3

    # Pairing survives a reload.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.users[USER]["api_endpoint"] == EU
    assert entry.runtime_data.users[USER]["devices"] == [DEVICE]


async def test_skill_id_restriction_and_bad_endpoint(hass, hass_client_no_auth, lwa, no_energy):
    lwa.post("https://api.amazonalexa.com/v1/datastore/commands", json={"results": []})
    entry = await _setup(hass, client_id="cid", client_secret="secret", skill_id="amzn1.ask.skill.mine")
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", json={"type": "launch", "user_id": USER, "skill_id": "amzn1.ask.skill.other"})
    assert resp.status == 403
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        json={"type": "launch", "user_id": USER, "skill_id": "amzn1.ask.skill.mine", "api_endpoint": "https://evil.example"},
    )
    assert resp.status == 200
    await hass.async_block_till_done()
    # An untrusted endpoint is replaced by the North America default and never called.
    assert entry.runtime_data.users[USER]["api_endpoint"] == "https://api.amazonalexa.com"
    assert not [c for c in lwa.mock_calls if "evil" in str(c[1])]
    assert entry.runtime_data.last_push_result == "accepted, no devices with the widget yet"


async def test_push_error_is_reported(hass, hass_client_no_auth, aioclient_mock, no_energy):
    aioclient_mock.post(LWA_TOKEN_URL, json={"access_token": "t", "expires_in": 3600})
    aioclient_mock.post(f"{EU}/v1/datastore/commands", status=403, json={"type": "DATA_STORE_SUPPORT_REQUIRED"})
    await _setup(hass, client_id="cid", client_secret="secret")
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{WEBHOOK_ID}", json={"type": "launch", "user_id": USER, "api_endpoint": EU})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.energy_widget_status")
    assert state.state == "error"
    assert "DATA_STORE_SUPPORT_REQUIRED" in state.attributes["last_error"]


async def test_options_flow_shows_webhook_url(hass, no_energy):
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["webhook_url"] == f"https://ha.example.com/api/webhook/{WEBHOOK_ID}"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "title": "Energy",
            "use_energy_dashboard": False,
            "entities": ["sensor.solar_power"],
            "entities_first": True,
            "update_interval": 120,
            "forget_accounts": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["update_interval"] == 120
    assert "forget_accounts" not in entry.options


async def test_diagnostics_redact(hass, hass_client, no_energy):
    from custom_components.alexa_energy_display.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, client_id="cid", client_secret="secret")
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)
    assert "secret" not in text.replace("client_secret", "") and WEBHOOK_ID not in text
