"""Build the display snapshot shared by the widget and the full-screen view.

The snapshot is plain JSON. Every value is pre-formatted, so the APL documents only
lay out text and never do arithmetic. Schema (version 1):

    {
      "v": 1,
      "title": "Energy",
      "updated": "14:05",                # local time of HA
      "updated_at": "2026-11-15T14:05:00+00:00",
      "status": "ok" | "empty",
      "tiles":  [Tile, ...],             # up to 12, full-screen view
      "widget": [Tile, ...],             # first 4 tiles, home screen widget
      "speech": "Solar 2.1 kilowatts..." # for screenless devices
    }

    Tile = {"id", "label", "value", "unit", "color", "sub"?, "percent"?}
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import MAX_TILES, SNAPSHOT_VERSION, WIDGET_TILES

_LOGGER = logging.getLogger(__name__)

COLORS = {
    "solar": "#F5B700",
    "import": "#FF6B5A",
    "export": "#3DD68C",
    "battery": "#4DA3FF",
    "home": "#B48CFF",
    "neutral": "#8FA3BF",
    "temperature": "#FF9F43",
    "monetary": "#3DD68C",
}

_SPOKEN_UNITS = {
    "kW": "kilowatts",
    "W": "watts",
    "kWh": "kilowatt hours",
    "Wh": "watt hours",
    "%": "percent",
}


@dataclass(slots=True)
class EnergySources:
    """Statistic and entity ids pulled from the Energy dashboard preferences."""

    solar_energy: list[str] = field(default_factory=list)
    solar_power: list[str] = field(default_factory=list)
    grid_import: list[str] = field(default_factory=list)
    grid_export: list[str] = field(default_factory=list)
    grid_power: list[str] = field(default_factory=list)
    battery_in: list[str] = field(default_factory=list)
    battery_out: list[str] = field(default_factory=list)
    battery_power: list[str] = field(default_factory=list)
    battery_soc: list[str] = field(default_factory=list)
    home_energy: list[str] = field(default_factory=list)
    home_power: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Return True when nothing usable is configured."""
        return not any(getattr(self, f) for f in self.__slots__)


def parse_energy_prefs(prefs: Mapping[str, Any] | None) -> EnergySources:
    """Extract ids from Energy preferences, legacy and unified grid formats alike."""
    out = EnergySources()
    if not prefs:
        return out

    def add(target: list[str], value: Any) -> None:
        if isinstance(value, str) and value and value not in target:
            target.append(value)

    for src in prefs.get("energy_sources", []):
        kind = src.get("type")
        if kind == "solar":
            add(out.solar_energy, src.get("stat_energy_from"))
            add(out.solar_power, src.get("stat_rate"))
        elif kind == "grid":
            # Unified format (2026+): one import/export pair per grid entry.
            add(out.grid_import, src.get("stat_energy_from"))
            add(out.grid_export, src.get("stat_energy_to"))
            add(out.grid_power, src.get("stat_rate"))
            # Legacy format: lists of flows.
            for flow in src.get("flow_from", []) or []:
                add(out.grid_import, flow.get("stat_energy_from"))
            for flow in src.get("flow_to", []) or []:
                add(out.grid_export, flow.get("stat_energy_to"))
            for power in src.get("power", []) or []:
                add(out.grid_power, power.get("stat_rate"))
        elif kind == "battery":
            add(out.battery_out, src.get("stat_energy_from"))
            add(out.battery_in, src.get("stat_energy_to"))
            add(out.battery_power, src.get("stat_rate"))
            add(out.battery_soc, src.get("stat_soc"))
        elif kind == "home":
            add(out.home_energy, src.get("stat_energy_to"))
            add(out.home_power, src.get("stat_rate"))
    return out


# ----------------------------------------------------------------------------- format


def _num(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    if text in ("-0", "-0.0", "-0.00"):
        text = text[1:]
    return text


def format_power(kw: float) -> tuple[str, str]:
    """Format kW for display: W below 1 kW, then 2 or 1 decimals."""
    magnitude = abs(kw)
    if magnitude < 1:
        return _num(kw * 1000, 0), "W"
    return _num(kw, 2 if magnitude < 10 else 1), "kW"


def format_energy(kwh: float) -> tuple[str, str]:
    """Format kWh for display."""
    magnitude = abs(kwh)
    if magnitude < 10:
        return _num(kwh, 2), "kWh"
    if magnitude < 1000:
        return _num(kwh, 1), "kWh"
    return _num(kwh / 1000, 2), "MWh"


def format_number(value: float) -> str:
    """Format an arbitrary numeric state."""
    magnitude = abs(value)
    if magnitude >= 100:
        return _num(value, 0)
    if magnitude >= 10:
        return _num(value, 1)
    return _num(value, 2)


def tile(
    tile_id: str,
    label: str,
    value: str,
    unit: str = "",
    color: str = COLORS["neutral"],
    sub: str | None = None,
    percent: float | None = None,
) -> dict[str, Any]:
    """Build one tile; optional keys are omitted so APL `when` checks stay simple."""
    out: dict[str, Any] = {
        "id": tile_id,
        "label": label[:28],
        "value": value[:12],
        "unit": unit[:8],
        "color": color,
    }
    if sub:
        out["sub"] = sub[:32]
    if percent is not None:
        out["percent"] = round(max(0.0, min(100.0, percent)), 1)
    return out


# ----------------------------------------------------------------------------- read


def _float_state(hass: HomeAssistant, entity_id: str) -> tuple[float, str | None] | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        return float(state.state), state.attributes.get("unit_of_measurement")
    except (TypeError, ValueError):
        return None


def read_power_kw(hass: HomeAssistant, entity_ids: Iterable[str]) -> float | None:
    """Sum power sensors in kW; None when none of them is readable."""
    total: float | None = None
    for entity_id in entity_ids:
        read = _float_state(hass, entity_id)
        if read is None:
            continue
        value, unit = read
        try:
            kw = PowerConverter.convert(value, unit or UnitOfPower.WATT, UnitOfPower.KILO_WATT)
        except Exception:  # noqa: BLE001 - unknown unit, skip this sensor
            _LOGGER.debug("Unsupported power unit %s on %s", unit, entity_id)
            continue
        total = kw if total is None else total + kw
    return total


def read_percent(hass: HomeAssistant, entity_ids: Iterable[str]) -> float | None:
    """Average of readable percentage sensors."""
    values = [r[0] for e in entity_ids if (r := _float_state(hass, e)) is not None]
    return sum(values) / len(values) if values else None


async def async_energy_today(hass: HomeAssistant, statistic_ids: Iterable[str]) -> float | None:
    """Sum today's change (kWh) of energy statistics, from local midnight."""
    ids = list(statistic_ids)
    if not ids or "recorder" not in hass.config.components:
        return None
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        statistic_during_period,
    )

    start = dt_util.start_of_local_day()
    total: float | None = None
    for stat_id in ids:
        try:
            result = await get_instance(hass).async_add_executor_job(
                statistic_during_period, hass, start, None, stat_id, {"change"}, {"energy": "kWh"}
            )
        except Exception:  # noqa: BLE001 - one bad statistic must not blank the display
            _LOGGER.debug("Statistic %s unavailable", stat_id, exc_info=True)
            continue
        change = (result or {}).get("change")
        if change is not None:
            total = float(change) if total is None else total + float(change)
    return total


# ----------------------------------------------------------------------------- build


async def async_energy_tiles(
    hass: HomeAssistant, src: EnergySources, metrics: dict[str, float] | None = None
) -> list[dict[str, Any]]:
    """Live power first, then today's totals.

    When `metrics` is given it is filled with the raw numbers behind the tiles
    (kW, kWh, %), for the Alexa smart home endpoints.
    """
    metrics = metrics if metrics is not None else {}
    live: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []

    solar_kw = read_power_kw(hass, src.solar_power)
    grid_kw = read_power_kw(hass, src.grid_power)
    battery_kw = read_power_kw(hass, src.battery_power)
    soc = read_percent(hass, src.battery_soc)
    home_kw = read_power_kw(hass, src.home_power)
    if home_kw is None and grid_kw is not None:
        home_kw = grid_kw + (solar_kw or 0.0) + (battery_kw or 0.0)
    for key, value in (
        ("solar_power_kw", None if solar_kw is None else max(solar_kw, 0.0)),
        ("grid_power_kw", grid_kw),
        ("battery_power_kw", battery_kw),
        ("battery_soc", soc),
        ("home_power_kw", None if home_kw is None else max(home_kw, 0.0)),
    ):
        if value is not None:
            metrics[key] = value

    if solar_kw is not None:
        live.append(tile("solar_now", "Solar", *format_power(max(solar_kw, 0.0)), COLORS["solar"], "now"))
    if grid_kw is not None:
        importing = grid_kw >= 0
        value, unit = format_power(abs(grid_kw))
        live.append(
            tile(
                "grid_now",
                "Grid import" if importing else "Grid export",
                value,
                unit,
                COLORS["import"] if importing else COLORS["export"],
                "now",
            )
        )
    if soc is not None or battery_kw is not None:
        if battery_kw is None or abs(battery_kw) < 0.01:
            sub = "idle"
        else:
            p_value, p_unit = format_power(abs(battery_kw))
            sub = f"{'discharging' if battery_kw > 0 else 'charging'} {p_value} {p_unit}"
        if soc is not None:
            live.append(tile("battery", "Battery", _num(soc, 0), "%", COLORS["battery"], sub, soc))
        else:
            live.append(tile("battery", "Battery", *format_power(abs(battery_kw)), COLORS["battery"], sub))
    if home_kw is not None:
        live.append(tile("home_now", "Home", *format_power(max(home_kw, 0.0)), COLORS["home"], "now"))

    solar_kwh = await async_energy_today(hass, src.solar_energy)
    import_kwh = await async_energy_today(hass, src.grid_import)
    export_kwh = await async_energy_today(hass, src.grid_export)
    batt_in = await async_energy_today(hass, src.battery_in)
    batt_out = await async_energy_today(hass, src.battery_out)
    home_kwh = await async_energy_today(hass, src.home_energy)
    if home_kwh is None and any(v is not None for v in (solar_kwh, import_kwh)):
        home_kwh = (
            (solar_kwh or 0.0)
            + (import_kwh or 0.0)
            - (export_kwh or 0.0)
            + (batt_out or 0.0)
            - (batt_in or 0.0)
        )

    for key, value in (
        ("solar_today_kwh", solar_kwh),
        ("import_today_kwh", import_kwh),
        ("export_today_kwh", export_kwh),
        ("battery_in_today_kwh", batt_in),
        ("battery_out_today_kwh", batt_out),
        ("home_today_kwh", None if home_kwh is None else max(home_kwh, 0.0)),
    ):
        if value is not None:
            metrics[key] = value

    if solar_kwh is not None:
        today.append(tile("solar_today", "Solar", *format_energy(solar_kwh), COLORS["solar"], "today"))
    if import_kwh is not None:
        today.append(tile("import_today", "Imported", *format_energy(import_kwh), COLORS["import"], "today"))
    if export_kwh is not None:
        today.append(tile("export_today", "Exported", *format_energy(export_kwh), COLORS["export"], "today"))
    if home_kwh is not None:
        today.append(tile("home_today", "Home used", *format_energy(max(home_kwh, 0.0)), COLORS["home"], "today"))
    if batt_in is not None or batt_out is not None:
        v_in, u_in = format_energy(batt_in or 0.0)
        v_out, _ = format_energy(batt_out or 0.0)
        today.append(
            tile("battery_today", "Battery in / out", f"{v_in}/{v_out}", u_in, COLORS["battery"], "today")
        )
    return live + today


def _entity_color(device_class: str | None, unit: str | None) -> str:
    if device_class == "battery":
        return COLORS["battery"]
    if device_class == "temperature":
        return COLORS["temperature"]
    if device_class == "monetary":
        return COLORS["monetary"]
    if device_class in ("power", "energy") or unit in ("W", "kW", "kWh", "Wh"):
        return COLORS["home"]
    return COLORS["neutral"]


def entity_tiles(hass: HomeAssistant, entity_ids: Iterable[str]) -> list[dict[str, Any]]:
    """One tile per arbitrary entity, using its friendly name and unit."""
    tiles: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            continue
        label = str(state.attributes.get("friendly_name") or state.object_id)
        unit = state.attributes.get("unit_of_measurement") or ""
        device_class = state.attributes.get("device_class")
        percent: float | None = None
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            value, unit = "—", ""
        else:
            try:
                number = float(state.state)
            except ValueError:
                value = state.state.replace("_", " ")
            else:
                value = format_number(number)
                if unit == "%":
                    percent = number
        tiles.append(
            tile(
                entity_id,
                label,
                value,
                unit,
                _entity_color(device_class, unit),
                None,
                percent,
            )
        )
    return tiles


def _speech(tiles: list[dict[str, Any]]) -> str:
    parts = []
    for t in tiles[:6]:
        unit = _SPOKEN_UNITS.get(t["unit"], t["unit"])
        when = f" {t['sub']}" if t.get("sub") in ("now", "today") else ""
        parts.append(f"{t['label']}{when}: {t['value'].replace('/', ' in, ')} {unit}".strip())
    return ". ".join(parts) + "." if parts else "There is no energy data to show yet."


def assemble(
    title: str,
    tiles: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Wrap tiles into the versioned snapshot document."""
    now = dt_util.as_local(now or dt_util.now())
    tiles = tiles[:MAX_TILES]
    return {
        "v": SNAPSHOT_VERSION,
        "title": title[:40],
        "updated": now.strftime("%H:%M"),
        "updated_at": now.isoformat(timespec="seconds"),
        "status": "ok" if tiles else "empty",
        "tiles": tiles,
        "widget": tiles[:WIDGET_TILES],
        "speech": _speech(tiles),
    }


def content_key(snapshot: Mapping[str, Any]) -> tuple:
    """What changed for push decisions; ignores timestamps."""
    return (
        snapshot.get("title"),
        tuple(tuple(sorted(t.items())) for t in snapshot.get("tiles", [])),
    )
