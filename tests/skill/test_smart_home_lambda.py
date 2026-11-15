"""Smart home Lambda forwarder."""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sh_lambda", Path(__file__).parents[2] / "smart-home/lambda/lambda_function.py"
)
sh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sh)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setenv("BASE_URL", "https://ha.example.com/")

    def urlopen(request, timeout):
        calls.append(request)
        return _Resp(json.dumps({"event": {"header": {"name": "ok"}}}).encode())

    monkeypatch.setattr(sh.urllib.request, "urlopen", urlopen)
    return calls


def d(namespace, name, **extra):
    return {"directive": {"header": {"namespace": namespace, "name": name, "payloadVersion": "3", "messageId": "m",
                                     "correlationToken": "c"}, **extra}}


@pytest.mark.parametrize(
    "event",
    [
        d("Alexa.Discovery", "Discover", payload={"scope": {"type": "BearerToken", "token": "TOK"}}),
        d("Alexa.Authorization", "AcceptGrant", payload={"grant": {"code": "x"}, "grantee": {"token": "TOK"}}),
        d("Alexa", "ReportState", endpoint={"endpointId": "e", "scope": {"token": "TOK"}}, payload={}),
    ],
)
def test_forwards_with_linked_token(sent, event):
    assert sh.lambda_handler(event) == {"event": {"header": {"name": "ok"}}}
    request = sent[0]
    assert request.full_url == "https://ha.example.com/api/alexa_energy_display/smart_home"
    assert request.get_header("Authorization") == "Bearer TOK"
    assert json.loads(request.data) == event


def test_errors(monkeypatch, sent):
    event = d("Alexa", "ReportState", endpoint={"endpointId": "e", "scope": {"token": "TOK"}}, payload={})

    def deny(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

    monkeypatch.setattr(sh.urllib.request, "urlopen", deny)
    out = sh.lambda_handler(event)["event"]
    assert out["payload"]["type"] == "INVALID_AUTHORIZATION_CREDENTIAL"
    assert out["header"]["correlationToken"] == "c" and out["endpoint"] == {"endpointId": "e"}

    def down(request, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(sh.urllib.request, "urlopen", down)
    assert sh.lambda_handler(event)["event"]["payload"]["type"] == "BRIDGE_UNREACHABLE"
    monkeypatch.delenv("BASE_URL")
    assert sh.lambda_handler(event)["event"]["payload"]["type"] == "INTERNAL_ERROR"
    assert sh.lambda_handler(d("Alexa.Discovery", "Discover", payload={}))["event"]["payload"]["type"] in (
        "INTERNAL_ERROR", "INVALID_AUTHORIZATION_CREDENTIAL")
