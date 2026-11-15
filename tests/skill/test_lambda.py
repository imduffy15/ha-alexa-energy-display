"""Skill handlers, driven through the real ask-sdk-core dispatcher."""

from __future__ import annotations

import json

import ha_client
import lambda_function
import pytest
import views

from .envelopes import (
    DATASTORE_ERROR,
    DEVICE,
    LAUNCH,
    REFRESH_EVENT,
    SKILL,
    USAGES_INSTALLED,
    USER,
    WIDGET_TAP,
    envelope,
    intent,
)

SNAPSHOT = {
    "v": 1,
    "title": "Energy",
    "updated": "14:05",
    "status": "ok",
    "tiles": [
        {"id": "solar_now", "label": "Solar", "value": "2.10", "unit": "kW", "color": "#F5B700", "sub": "now"},
        {"id": "battery", "label": "Battery", "value": "76", "unit": "%", "color": "#4DA3FF", "percent": 76},
    ],
    "widget": [],
    "speech": "Solar now: 2.10 kilowatts.",
}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_call(event_type, ident, detail=None):
        seen.append((event_type, ident, detail))
        return {"snapshot": SNAPSHOT, "dashboard_url": "https://ha.example.com/energy", "widget": True}

    monkeypatch.setattr(ha_client, "call", fake_call)
    return seen


def run(request, **kw):
    return lambda_function.lambda_handler(envelope(request, **kw), None)["response"]


def directive_types(resp):
    return [d["type"] for d in resp.get("directives", [])]


def test_launch_renders_fullscreen_and_pairs(calls):
    resp = run(LAUNCH)
    assert directive_types(resp) == ["Alexa.Presentation.APL.RenderDocument"]
    directive = resp["directives"][0]
    energy = directive["datasources"]["energy"]
    assert energy["rows"] == [SNAPSHOT["tiles"]]
    assert energy["status"] == "ok"
    assert energy["tileWidth"] == "50.0000%"
    assert directive["document"]["version"] == "2024.1"
    assert "shouldEndSession" not in resp  # stays on screen, mic closed
    assert "outputSpeech" not in resp
    event, ident, _ = calls[0]
    assert event == "launch"
    assert ident == {
        "user_id": USER,
        "device_id": DEVICE,
        "api_endpoint": "https://api.eu.amazonalexa.com",
        "skill_id": SKILL,
    }


def test_screenless_device_speaks(calls):
    resp = run(LAUNCH, screen=False)
    assert "directives" not in resp or not resp["directives"]
    assert "2.10 kilowatts" in resp["outputSpeech"]["ssml"]
    assert resp["shouldEndSession"] is True


def test_refresh_tick_and_widget_tap(calls):
    run(REFRESH_EVENT)
    run(WIDGET_TAP)
    run(intent("RefreshIntent"))
    assert [c[0] for c in calls] == ["refresh", "launch", "refresh"]


def test_open_dashboard(calls):
    resp = run(intent("OpenDashboardIntent"))
    assert directive_types(resp) == [
        "Alexa.Presentation.APL.RenderDocument",
        "Alexa.Presentation.APL.ExecuteCommands",
    ]
    assert resp["directives"][1]["commands"][0] == {"type": "OpenURL", "source": "https://ha.example.com/energy"}


def test_open_dashboard_without_url(monkeypatch):
    monkeypatch.setattr(ha_client, "call", lambda *a, **k: {"snapshot": SNAPSHOT, "dashboard_url": None})
    resp = run(intent("OpenDashboardIntent"))
    assert "No dashboard URL" in resp["outputSpeech"]["ssml"]


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(ha_client, "_HERE", str(tmp_path))
    resp = run(LAUNCH)
    assert "not set up" in resp["outputSpeech"]["ssml"]
    energy = resp["directives"][0]["datasources"]["energy"]
    assert energy["status"] == "message" and "webhook" in energy["message"]


def test_home_assistant_down(monkeypatch):
    def boom(*a, **k):
        raise ha_client.HomeAssistantError("down")

    monkeypatch.setattr(ha_client, "call", boom)
    resp = run(LAUNCH)
    assert "couldn't reach Home Assistant" in resp["outputSpeech"]["ssml"]
    assert resp["shouldEndSession"] is True


def test_widget_lifecycle_events(calls):
    run(USAGES_INSTALLED, session=False)
    run(DATASTORE_ERROR, session=False)
    assert calls[0][0] == "widget_installed"
    assert calls[1][0] == "datastore_error"
    assert calls[1][2] == {"type": "DEVICE_UNAVAILABLE"}


def test_help_and_stop(calls):
    assert "open energy display" in run(intent("AMAZON.HelpIntent"))["outputSpeech"]["ssml"]
    assert run(intent("AMAZON.StopIntent"))["shouldEndSession"] is True
    assert calls == []


TILES = [{"id": str(i), "label": f"T{i}", "value": "1", "unit": "kW", "color": "#fff"} for i in range(12)]

# (name, width, height, shape, mode, tiles, expected columns, expected rows)
DEVICES = [
    ("Echo Show 5", 960, 480, "RECTANGLE", "HUB", 9, 3, 3),
    ("Echo Show 8/10", 1280, 800, "RECTANGLE", "HUB", 9, 3, 3),
    ("Echo Show 8 four tiles", 1280, 800, "RECTANGLE", "HUB", 4, 2, 2),
    ("Echo Show 15 landscape", 1920, 1080, "RECTANGLE", "HUB", 12, 4, 3),
    ("Echo Show 15 portrait", 1080, 1920, "RECTANGLE", "HUB", 9, 2, 5),
    ("Echo Show 15 portrait six", 1080, 1920, "RECTANGLE", "HUB", 6, 2, 3),
    ("Echo Spot (2017, round)", 480, 480, "ROUND", "HUB", 9, 2, 2),
    ("Echo Spot (2024)", 320, 240, "RECTANGLE", "HUB", 9, 2, 2),
    ("Fire TV", 1920, 1080, "RECTANGLE", "TV", 9, 3, 3),
    ("Fire tablet portrait", 800, 1280, "RECTANGLE", "MOBILE", 9, 2, 5),
    ("one tile", 1280, 800, "RECTANGLE", "HUB", 1, 1, 1),
]


@pytest.mark.parametrize(("name", "w", "h", "shape", "mode", "count", "cols", "rows"), DEVICES, ids=[d[0] for d in DEVICES])
def test_layout_per_device(name, w, h, shape, mode, count, cols, rows):
    screen = views.Screen(w, h, shape, mode)
    out = views.layout(TILES[:count], screen)
    assert len(out["rows"]) == rows
    assert max(len(r) for r in out["rows"]) == cols
    assert out["tileWidth"] == f"{100 / cols:.4f}%"
    # Text must fit its cell: value height (vh) under the row height, and the
    # value's width estimate (6 chars at ~0.6 em) under the cell width.
    value_vh = float(out["valueSize"].rstrip("vh"))
    row_vh = float(out["rowHeight"].rstrip("vh"))
    assert value_vh * 2.2 < row_vh, name
    cell_px = w * 0.94 / cols
    assert value_vh / 100 * h * 0.6 * 4 < cell_px, name


@pytest.mark.parametrize(
    ("device", "expected"), [(None, "1.3"), ("1.3", "1.3"), ("1.8", "1.8"), ("2023.2", "2023.2"), ("2024.3", "2024.1"), ("1.0", "1.3")]
)
def test_document_version_follows_device(device, expected):
    assert views.document_version(device) == expected


def test_old_renderer_gets_old_version(calls):
    env = envelope(LAUNCH)
    env["context"]["System"]["device"]["supportedInterfaces"]["Alexa.Presentation.APL"]["runtime"]["maxVersion"] = "1.3"
    env["context"]["Viewport"] = {"shape": "ROUND", "pixelWidth": 480, "pixelHeight": 480, "currentPixelWidth": 480,
                                  "currentPixelHeight": 480, "dpi": 160, "mode": "HUB", "touch": ["SINGLE"]}
    resp = lambda_function.lambda_handler(env, None)["response"]
    directive = resp["directives"][0]
    assert directive["document"]["version"] == "1.3"
    assert directive["datasources"]["energy"]["sidePadding"] == "15vw"
    assert directive["datasources"]["energy"]["showUpdated"] is False


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ha_client_request(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"webhook_url": "https://ha.example.com/api/webhook/x"}))
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(ha_client, "_HERE", str(tmp_path))
    sent = {}

    def fake_urlopen(request, timeout):
        sent.update(url=request.full_url, method=request.get_method(), timeout=timeout,
                    body=json.loads(request.data), ctype=request.get_header("Content-type"))
        return _Resp(json.dumps({"snapshot": SNAPSHOT}).encode())

    monkeypatch.setattr(ha_client.urllib.request, "urlopen", fake_urlopen)
    reply = ha_client.call("launch", {"user_id": USER}, {"k": 1})
    assert reply["snapshot"]["title"] == "Energy"
    assert sent == {
        "url": "https://ha.example.com/api/webhook/x",
        "method": "POST",
        "timeout": 6.0,
        "body": {"type": "launch", "user_id": USER, "detail": {"k": 1}},
        "ctype": "application/json",
    }

    def refuse(request, timeout):
        raise ha_client.urllib.error.URLError("refused")

    monkeypatch.setattr(ha_client.urllib.request, "urlopen", refuse)
    with pytest.raises(ha_client.HomeAssistantError):
        ha_client.call("launch", {})


def test_env_overrides_config(monkeypatch, tmp_path):
    monkeypatch.setattr(ha_client, "_HERE", str(tmp_path))
    monkeypatch.setenv("WEBHOOK_URL", "https://env.example.com/api/webhook/y")
    assert ha_client.load_config()["webhook_url"] == "https://env.example.com/api/webhook/y"


def test_config_rejects_placeholder(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(
        open(f"{ha_client._HERE}/config.example.json").read()  # noqa: SIM115
    )
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(ha_client, "_HERE", str(tmp_path))
    with pytest.raises(ha_client.NotConfigured):
        ha_client.load_config()
