"""Alexa Smart Home endpoint: energy values as read-only Alexa devices.

The smart home skill's Lambda (smart-home/lambda/) forwards every directive here with
the account-linked Home Assistant access token, so Home Assistant authenticates the
call itself (`requires_auth`).

Each config entry contributes these devices, named with the entry's voice prefix:

    Solar     RangeController Solar.Power (W), Solar.Today (kWh)
    Grid      RangeController Grid.Power (W, + import / - export),
              Grid.ImportToday, Grid.ExportToday (kWh); ModeController Grid.Direction
    Battery   RangeController Battery.Level (%), Battery.Power (W);
              ModeController Battery.State (charging / discharging / idle)
    Home      RangeController Home.Power (W), Home.Today (kWh)
    <entity>  each extra numeric entity: TemperatureSensor for temperatures, otherwise
              RangeController Entity.Value (with Alexa.Unit.Percent for %)

Every controller is `nonControllable`: Alexa can report values but never change them.
Mode changes and whole-percent battery level changes are sent as ChangeReports when the
skill granted event permission (AcceptGrant), which lets Alexa routines trigger on them.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITIES,
    CONF_SH_CLIENT_ID,
    CONF_SH_CLIENT_SECRET,
    CONF_VOICE_PREFIX,
    DEFAULT_API_ENDPOINT,
    DOMAIN,
    LWA_TOKEN_URL,
    SMART_HOME_PATH,
)

if TYPE_CHECKING:
    from .coordinator import EnergyDisplayCoordinator

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MANUFACTURER = "Alexa Energy Display"
_MAX_W = 1_000_000
_MAX_KWH = 1_000_000


def _text(*names: str) -> list[dict[str, Any]]:
    """Friendly names for every English locale."""
    return [
        {"@type": "text", "value": {"text": name, "locale": locale}}
        for name in names
        for locale in ("en-US", "en-GB", "en-AU", "en-CA", "en-IN")
    ]


@dataclass(slots=True)
class Range:
    instance: str
    names: tuple[str, ...]
    metric: str  # key in coordinator.metrics, or "entity:<entity_id>"
    minimum: float
    maximum: float
    precision: float
    scale: float = 1.0  # metric * scale = reported value
    unit: str | None = None
    proactive: bool = False


@dataclass(slots=True)
class Mode:
    instance: str
    names: tuple[str, ...]
    modes: dict[str, tuple[str, ...]]  # value -> friendly names
    resolve: Any  # callable(metrics) -> mode value or None
    proactive: bool = True


@dataclass(slots=True)
class Device:
    endpoint_id: str
    name: str
    description: str
    category: str = "OTHER"
    ranges: list[Range] = field(default_factory=list)
    modes: list[Mode] = field(default_factory=list)
    temperature: str | None = None  # entity_id for Alexa.TemperatureSensor


def _battery_state(m: dict[str, float]) -> str | None:
    power = m.get("battery_power_kw")
    if power is None:
        return None
    if abs(power) < 0.02:
        return "Battery.State.Idle"
    return "Battery.State.Discharging" if power > 0 else "Battery.State.Charging"


def _grid_direction(m: dict[str, float]) -> str | None:
    power = m.get("grid_power_kw")
    if power is None:
        return None
    if abs(power) < 0.02:
        return "Grid.Direction.Idle"
    return "Grid.Direction.Importing" if power > 0 else "Grid.Direction.Exporting"


def build_devices(hass: HomeAssistant, coordinator: EnergyDisplayCoordinator) -> list[Device]:
    """Devices for one config entry, from the metrics its last snapshot produced."""
    entry = coordinator.config_entry
    prefix = (entry.options.get(CONF_VOICE_PREFIX) or "").strip()
    base = entry.entry_id
    m = coordinator.metrics
    src = coordinator.energy_sources
    # Devices follow what is configured, not what happens to be readable right now, so
    # a sensor that is briefly unavailable (after a restart, say) doesn't make Alexa
    # forget the device. Missing values are reported as connectivity UNREACHABLE.
    configured = {
        "solar_power_kw": src.solar_power,
        "solar_today_kwh": src.solar_energy,
        "grid_power_kw": src.grid_power,
        "import_today_kwh": src.grid_import,
        "export_today_kwh": src.grid_export,
        "battery_soc": src.battery_soc,
        "battery_power_kw": src.battery_power,
        "home_power_kw": src.home_power or src.grid_power,
        "home_today_kwh": src.home_energy or src.solar_energy or src.grid_import,
    }

    def has(metric: str) -> bool:
        return metric in m or bool(configured.get(metric))

    devices: list[Device] = []

    def named(name: str) -> str:
        return f"{prefix} {name}".strip()

    def kwh(instance: str, names: tuple[str, ...], metric: str) -> Range:
        return Range(instance, names, metric, 0, _MAX_KWH, 0.01)

    def watts(instance: str, names: tuple[str, ...], metric: str, signed: bool = False) -> Range:
        return Range(instance, names, metric, -_MAX_W if signed else 0, _MAX_W, 1, scale=1000)

    solar = [r for r in (
        watts("Solar.Power", ("Power", "Solar power", "Generation"), "solar_power_kw")
        if has("solar_power_kw") else None,
        kwh("Solar.Today", ("Energy today", "Generation today", "Solar today"), "solar_today_kwh")
        if has("solar_today_kwh") else None,
    ) if r]
    if solar:
        devices.append(Device(f"{base}-solar", named("Solar"), "Solar generation from Home Assistant", ranges=solar))

    grid = [r for r in (
        watts("Grid.Power", ("Power", "Grid power"), "grid_power_kw", signed=True) if has("grid_power_kw") else None,
        kwh("Grid.ImportToday", ("Import today", "Imported today"), "import_today_kwh")
        if has("import_today_kwh") else None,
        kwh("Grid.ExportToday", ("Export today", "Exported today"), "export_today_kwh")
        if has("export_today_kwh") else None,
    ) if r]
    grid_modes = []
    if has("grid_power_kw"):
        grid_modes.append(Mode(
            "Grid.Direction", ("Direction", "Grid direction"),
            {"Grid.Direction.Importing": ("Importing",), "Grid.Direction.Exporting": ("Exporting",),
             "Grid.Direction.Idle": ("Idle",)},
            _grid_direction,
        ))
    if grid or grid_modes:
        devices.append(Device(f"{base}-grid", named("Grid"), "Grid import and export from Home Assistant",
                              ranges=grid, modes=grid_modes))

    battery = [r for r in (
        Range("Battery.Level", ("Level", "Charge", "Battery level"), "battery_soc", 0, 100, 1,
              unit="Alexa.Unit.Percent", proactive=True) if has("battery_soc") else None,
        watts("Battery.Power", ("Power", "Battery power"), "battery_power_kw", signed=True)
        if has("battery_power_kw") else None,
    ) if r]
    battery_modes = []
    if has("battery_power_kw"):
        battery_modes.append(Mode(
            "Battery.State", ("State", "Battery state"),
            {"Battery.State.Charging": ("Charging",), "Battery.State.Discharging": ("Discharging",),
             "Battery.State.Idle": ("Idle",)},
            _battery_state,
        ))
    if battery or battery_modes:
        devices.append(Device(f"{base}-battery", named("Battery"), "Home battery from Home Assistant",
                              ranges=battery, modes=battery_modes))

    home = [r for r in (
        watts("Home.Power", ("Power", "Consumption", "Usage"), "home_power_kw") if has("home_power_kw") else None,
        kwh("Home.Today", ("Energy today", "Usage today", "Consumption today"), "home_today_kwh")
        if has("home_today_kwh") else None,
    ) if r]
    if home:
        devices.append(Device(f"{base}-home", named("Home Energy"), "Home consumption from Home Assistant",
                              ranges=home))

    for entity_id in entry.options.get(CONF_ENTITIES, []):
        state = hass.states.get(entity_id)
        if state is None:
            continue
        name = str(state.attributes.get("friendly_name") or state.object_id)
        unit = state.attributes.get("unit_of_measurement")
        device_id = f"{base}-entity-{entity_id}".replace(".", "_")
        if unit in ("°C", "°F"):
            devices.append(Device(device_id, named(name), f"{name} from Home Assistant",
                                  category="TEMPERATURE_SENSOR", temperature=entity_id))
            continue
        try:
            float(state.state)
        except ValueError:
            if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue  # text states have no Alexa sensor equivalent
        percent = unit == "%"
        devices.append(Device(device_id, named(name), f"{name} from Home Assistant", ranges=[
            Range("Entity.Value", ("Value", "Level") if percent else ("Value", "Reading"),
                  f"entity:{entity_id}", 0 if percent else -_MAX_W, 100 if percent else _MAX_W,
                  1 if percent else 0.01, unit="Alexa.Unit.Percent" if percent else None,
                  proactive=percent)
        ]))
    return devices


# ----------------------------------------------------------------------------- values


def range_value(hass: HomeAssistant, coordinator: EnergyDisplayCoordinator, rng: Range) -> float | None:
    if rng.metric.startswith("entity:"):
        state = hass.states.get(rng.metric[7:])
        try:
            value = float(state.state) if state else None
        except ValueError:
            value = None
    else:
        raw = coordinator.metrics.get(rng.metric)
        value = None if raw is None else raw * rng.scale
    if value is None:
        return None
    value = min(max(value, rng.minimum), rng.maximum)
    steps = round(value / rng.precision)
    value = steps * rng.precision
    return int(value) if rng.precision >= 1 else round(value, 2)


def _sample(seconds: int) -> dict[str, Any]:
    return {
        "timeOfSample": dt_util.utcnow().strftime("%Y-%m-%dT%H:%M:%S.00Z"),
        "uncertaintyInMilliseconds": seconds * 1000,
    }


def device_properties(
    hass: HomeAssistant, coordinator: EnergyDisplayCoordinator, device: Device
) -> list[dict[str, Any]]:
    uncertainty = int(coordinator.update_interval.total_seconds()) if coordinator.update_interval else 60
    props: list[dict[str, Any]] = []
    for rng in device.ranges:
        value = range_value(hass, coordinator, rng)
        if value is not None:
            props.append({"namespace": "Alexa.RangeController", "instance": rng.instance,
                          "name": "rangeValue", "value": value, **_sample(uncertainty)})
    for mode in device.modes:
        value = mode.resolve(coordinator.metrics)
        if value is not None:
            props.append({"namespace": "Alexa.ModeController", "instance": mode.instance,
                          "name": "mode", "value": value, **_sample(uncertainty)})
    if device.temperature:
        state = hass.states.get(device.temperature)
        try:
            temp = float(state.state) if state else None
        except ValueError:
            temp = None
        if temp is not None:
            scale = "FAHRENHEIT" if state.attributes.get("unit_of_measurement") == "°F" else "CELSIUS"
            props.append({"namespace": "Alexa.TemperatureSensor", "name": "temperature",
                          "value": {"value": round(temp, 1), "scale": scale}, **_sample(uncertainty)})
    healthy = bool(props)
    props.append({"namespace": "Alexa.EndpointHealth", "name": "connectivity",
                  "value": {"value": "OK" if healthy else "UNREACHABLE"}, **_sample(uncertainty)})
    return props


# ----------------------------------------------------------------------------- discovery


def discovery_endpoint(device: Device) -> dict[str, Any]:
    caps: list[dict[str, Any]] = [{"type": "AlexaInterface", "interface": "Alexa", "version": "3"}]
    for rng in device.ranges:
        config: dict[str, Any] = {
            "supportedRange": {"minimumValue": rng.minimum, "maximumValue": rng.maximum, "precision": rng.precision}
        }
        if rng.unit:
            config["unitOfMeasure"] = rng.unit
        caps.append({
            "type": "AlexaInterface", "interface": "Alexa.RangeController", "version": "3", "instance": rng.instance,
            "properties": {"supported": [{"name": "rangeValue"}], "proactivelyReported": rng.proactive,
                           "retrievable": True, "nonControllable": True},
            "capabilityResources": {"friendlyNames": _text(*rng.names)},
            "configuration": config,
        })
    for mode in device.modes:
        caps.append({
            "type": "AlexaInterface", "interface": "Alexa.ModeController", "version": "3", "instance": mode.instance,
            "properties": {"supported": [{"name": "mode"}], "proactivelyReported": mode.proactive,
                           "retrievable": True, "nonControllable": True},
            "capabilityResources": {"friendlyNames": _text(*mode.names)},
            "configuration": {"ordered": False, "supportedModes": [
                {"value": value, "modeResources": {"friendlyNames": _text(*names)}}
                for value, names in mode.modes.items()
            ]},
        })
    if device.temperature:
        caps.append({"type": "AlexaInterface", "interface": "Alexa.TemperatureSensor", "version": "3",
                     "properties": {"supported": [{"name": "temperature"}], "proactivelyReported": False,
                                    "retrievable": True}})
    caps.append({"type": "AlexaInterface", "interface": "Alexa.EndpointHealth", "version": "3",
                 "properties": {"supported": [{"name": "connectivity"}], "proactivelyReported": False,
                                "retrievable": True}})
    return {
        "endpointId": device.endpoint_id[:256],
        "manufacturerName": _MANUFACTURER,
        "description": device.description[:128],
        "friendlyName": "".join(c for c in device.name if c.isalnum() or c == " ")[:128] or "Energy",
        "displayCategories": [device.category],
        "capabilities": caps,
    }


# ----------------------------------------------------------------------------- messages


def _header(namespace: str, name: str, correlation: str | None = None) -> dict[str, Any]:
    header = {"namespace": namespace, "name": name, "payloadVersion": "3", "messageId": str(uuid.uuid4())}
    if correlation:
        header["correlationToken"] = correlation
    return header


def error_response(directive: dict[str, Any], kind: str, message: str) -> dict[str, Any]:
    header = directive.get("header", {})
    event: dict[str, Any] = {
        "header": _header("Alexa", "ErrorResponse", header.get("correlationToken")),
        "payload": {"type": kind, "message": message},
    }
    if "endpoint" in directive:
        event["endpoint"] = {"endpointId": directive["endpoint"].get("endpointId")}
    return {"event": event}


class GrantStore:
    """LWA tokens from AcceptGrant, used for proactive ChangeReports."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.smart_home_grant")
        self.data: dict[str, Any] = {}
        self._access: str | None = None
        self._expiry = 0.0

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        async with session.post(LWA_TOKEN_URL, data=form, timeout=_TIMEOUT) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in (body or {}):
                raise RuntimeError(f"LWA HTTP {resp.status}: {(body or {}).get('error')}")
            return body

    async def async_accept(self, code: str, client_id: str, client_secret: str) -> None:
        body = await self._token_request({
            "grant_type": "authorization_code", "code": code,
            "client_id": client_id, "client_secret": client_secret,
        })
        self.data = {"refresh_token": body["refresh_token"], "client_id": client_id}
        self._access = body["access_token"]
        self._expiry = time.monotonic() + int(body.get("expires_in", 3600)) - 120
        await self._store.async_save(self.data)

    async def async_access_token(self, client_id: str, client_secret: str) -> str | None:
        if not self.data.get("refresh_token") or self.data.get("client_id") != client_id:
            return None
        if self._access and time.monotonic() < self._expiry:
            return self._access
        body = await self._token_request({
            "grant_type": "refresh_token", "refresh_token": self.data["refresh_token"],
            "client_id": client_id, "client_secret": client_secret,
        })
        self._access = body["access_token"]
        self._expiry = time.monotonic() + int(body.get("expires_in", 3600)) - 120
        if body.get("refresh_token") and body["refresh_token"] != self.data["refresh_token"]:
            self.data["refresh_token"] = body["refresh_token"]
            await self._store.async_save(self.data)
        return self._access

    def invalidate(self) -> None:
        self._access = None


class SmartHome:
    """Directive handling across every loaded config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.grants = GrantStore(hass)
        self._reported: dict[tuple[str, str], Any] = {}

    def _coordinators(self) -> list[EnergyDisplayCoordinator]:
        return [e.runtime_data for e in self.hass.config_entries.async_loaded_entries(DOMAIN)]

    def devices(self) -> list[tuple[EnergyDisplayCoordinator, Device]]:
        return [(c, d) for c in self._coordinators() for d in build_devices(self.hass, c)]

    def _credentials(self) -> tuple[str, str] | None:
        for coordinator in self._coordinators():
            options = coordinator.config_entry.options
            if options.get(CONF_SH_CLIENT_ID) and options.get(CONF_SH_CLIENT_SECRET):
                return options[CONF_SH_CLIENT_ID], options[CONF_SH_CLIENT_SECRET]
        return None

    async def async_handle(self, message: dict[str, Any]) -> dict[str, Any]:
        directive = message.get("directive") or {}
        header = directive.get("header") or {}
        namespace, name = header.get("namespace"), header.get("name")
        if header.get("payloadVersion") != "3":
            return error_response(directive, "INVALID_DIRECTIVE", "payloadVersion 3 only")

        if namespace == "Alexa.Discovery" and name == "Discover":
            for coordinator in self._coordinators():
                if not coordinator.metrics and coordinator.data is None:
                    await coordinator.async_refresh()
            endpoints = [discovery_endpoint(d) for _, d in self.devices()]
            _LOGGER.info("Alexa discovery: %d energy devices", len(endpoints))
            return {"event": {"header": _header("Alexa.Discovery", "Discover.Response"),
                              "payload": {"endpoints": endpoints}}}

        if namespace == "Alexa.Authorization" and name == "AcceptGrant":
            creds = self._credentials()
            if creds:
                try:
                    await self.grants.async_accept(directive["payload"]["grant"]["code"], *creds)
                except (KeyError, RuntimeError, aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.warning("AcceptGrant failed: %s", err)
                    return {"event": {"header": _header("Alexa.Authorization", "ErrorResponse"),
                                      "payload": {"type": "ACCEPT_GRANT_FAILED", "message": str(err)[:200]}}}
                _LOGGER.info("Alexa granted event permission; proactive reports enabled")
            else:
                _LOGGER.info("AcceptGrant received, but no smart home skill credentials are configured")
            return {"event": {"header": _header("Alexa.Authorization", "AcceptGrant.Response"), "payload": {}}}

        endpoint_id = (directive.get("endpoint") or {}).get("endpointId")
        match = next(((c, d) for c, d in self.devices() if d.endpoint_id == endpoint_id), None)
        if match is None:
            return error_response(directive, "NO_SUCH_ENDPOINT", f"unknown endpoint {endpoint_id}")
        coordinator, device = match

        if namespace == "Alexa" and name == "ReportState":
            return {
                "event": {
                    "header": _header("Alexa", "StateReport", header.get("correlationToken")),
                    "endpoint": {"scope": directive["endpoint"].get("scope"), "endpointId": endpoint_id},
                    "payload": {},
                },
                "context": {"properties": device_properties(self.hass, coordinator, device)},
            }
        return error_response(
            directive, "NOT_SUPPORTED_IN_CURRENT_MODE", f"{device.name} only reports values; it cannot be changed"
        )

    async def async_report_changes(self) -> None:
        """Send ChangeReports for proactively reported properties that changed."""
        creds = self._credentials()
        if not creds:
            return
        changes: list[tuple[str, dict[str, Any]]] = []
        for coordinator, device in self.devices():
            props = device_properties(self.hass, coordinator, device)
            proactive = {r.instance for r in device.ranges if r.proactive} | {
                m.instance for m in device.modes if m.proactive
            }
            for prop in props:
                if prop.get("instance") not in proactive:
                    continue
                key = (device.endpoint_id, prop["instance"])
                if self._reported.get(key, prop["value"]) != prop["value"]:
                    changes.append((device.endpoint_id, prop))
                self._reported[key] = prop["value"]
        if not changes:
            return
        try:
            token = await self.grants.async_access_token(*creds)
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Could not refresh the Alexa event token: %s", err)
            return
        if not token:
            return
        gateway = f"{self._api_endpoint()}/v3/events"
        session = async_get_clientsession(self.hass)
        for endpoint_id, prop in changes:
            body = {
                "event": {
                    "header": _header("Alexa", "ChangeReport"),
                    "endpoint": {"scope": {"type": "BearerToken", "token": token}, "endpointId": endpoint_id},
                    "payload": {"change": {"cause": {"type": "PERIODIC_POLL"}, "properties": [prop]}},
                }
            }
            try:
                async with session.post(gateway, json=body, headers={"Authorization": f"Bearer {token}"},
                                        timeout=_TIMEOUT) as resp:
                    if resp.status == 401:
                        self.grants.invalidate()
                    elif resp.status not in (200, 202):
                        _LOGGER.debug("ChangeReport HTTP %s: %s", resp.status, (await resp.text())[:200])
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("ChangeReport failed: %s", err)

    def _api_endpoint(self) -> str:
        for coordinator in self._coordinators():
            for info in coordinator.users.values():
                if info.get("api_endpoint"):
                    return info["api_endpoint"]
        return DEFAULT_API_ENDPOINT


class SmartHomeView(HomeAssistantView):
    """POST target for the smart home Lambda. Authenticated with the linked HA token."""

    url = SMART_HOME_PATH
    name = f"api:{DOMAIN}:smart_home"
    requires_auth = True

    def __init__(self, smart_home: SmartHome) -> None:
        self.smart_home = smart_home

    async def post(self, request: web.Request) -> web.Response:
        try:
            message = await request.json()
        except ValueError:
            return self.json_message("invalid JSON", HTTPStatus.BAD_REQUEST)
        if not isinstance(message, dict):
            return self.json_message("invalid JSON", HTTPStatus.BAD_REQUEST)
        return self.json(await self.smart_home.async_handle(message))


REPORT_INTERVAL = timedelta(seconds=60)
