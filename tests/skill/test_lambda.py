"""Skill handlers, driven through the real ask-sdk-core dispatcher."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    payload = resp["directives"][0]["datasources"]["payload"]
    assert payload["tiles"] == SNAPSHOT["tiles"]
    assert payload["status"] == "ok"
    assert payload["tileWidth"] == "50.0000%" and payload["tileHeight"] == "100.0000%"
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
    payload = resp["directives"][0]["datasources"]["payload"]
    assert payload["status"] == "message" and "webhook" in payload["message"]


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


@pytest.mark.parametrize(
    ("count", "cols", "rows"),
    [(0, 1, 1), (1, 1, 1), (4, 2, 2), (5, 3, 2), (9, 3, 3), (12, 4, 3), (40, 4, 3)],
)
def test_layout(count, cols, rows):
    out = views.layout(count)
    assert out["tileWidth"] == f"{100 / cols:.4f}%"
    assert out["tileHeight"] == f"{100 / rows:.4f}%"


class _Handler(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.received.append(body)
        out = json.dumps({"snapshot": SNAPSHOT}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_ha_client_http_roundtrip(monkeypatch, tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # https is required in config; call urllib against http via env override of the check.
        (tmp_path / "config.json").write_text(json.dumps({"webhook_url": "https://placeholder"}))
        monkeypatch.setattr(ha_client, "_HERE", str(tmp_path))
        config = ha_client.load_config()
        assert config["webhook_url"] == "https://placeholder"
        monkeypatch.setattr(
            ha_client, "load_config", lambda: {"webhook_url": f"http://127.0.0.1:{server.server_port}/api/webhook/x"}
        )
        reply = ha_client.call("launch", {"user_id": USER}, {"k": 1})
        assert reply["snapshot"]["title"] == "Energy"
        assert _Handler.received[-1] == {"type": "launch", "user_id": USER, "detail": {"k": 1}}
    finally:
        server.shutdown()


def test_config_rejects_placeholder(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(
        open(f"{ha_client._HERE}/config.example.json").read()  # noqa: SIM115
    )
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setattr(ha_client, "_HERE", str(tmp_path))
    with pytest.raises(ha_client.NotConfigured):
        ha_client.load_config()
