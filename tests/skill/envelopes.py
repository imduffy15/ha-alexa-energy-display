"""Minimal Alexa request envelopes, shaped like real Echo Show requests."""

from __future__ import annotations

from typing import Any

USER = "amzn1.ask.account.TESTUSER"
DEVICE = "amzn1.ask.device.TESTDEVICE"
SKILL = "amzn1.ask.skill.00000000-0000-0000-0000-000000000000"
APL = {"Alexa.Presentation.APL": {"runtime": {"maxVersion": "2024.1"}}}


def envelope(request: dict[str, Any], screen: bool = True, session: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": "1.0",
        "context": {
            "System": {
                "application": {"applicationId": SKILL},
                "user": {"userId": USER},
                "device": {"deviceId": DEVICE, "supportedInterfaces": APL if screen else {}},
                "apiEndpoint": "https://api.eu.amazonalexa.com",
                "apiAccessToken": "token",
            }
        },
        "request": {"requestId": "r1", "timestamp": "2026-11-15T12:00:00Z", "locale": "en-GB", **request},
    }
    if session:
        body["session"] = {
            "new": True,
            "sessionId": "s1",
            "application": {"applicationId": SKILL},
            "user": {"userId": USER},
        }
    return body


def intent(name: str) -> dict[str, Any]:
    return {"type": "IntentRequest", "intent": {"name": name, "confirmationStatus": "NONE"}}


LAUNCH = {"type": "LaunchRequest"}
REFRESH_EVENT = {"type": "Alexa.Presentation.APL.UserEvent", "token": "energyDisplay", "arguments": ["refresh"]}
WIDGET_TAP = {"type": "Alexa.Presentation.APL.UserEvent", "arguments": ["open"]}
USAGES_INSTALLED = {
    "type": "Alexa.DataStore.PackageManager.UsagesInstalled",
    "payload": {
        "packageId": "EnergyWidget",
        "packageVersion": "1.0.0",
        "usages": [{"instanceId": "amzn1.ask.package.v1.instance.v1.x", "location": "FAVORITE"}],
    },
}
DATASTORE_ERROR = {
    "type": "Alexa.DataStore.Error",
    "error": {"type": "DEVICE_UNAVAILABLE", "content": {"deviceId": DEVICE, "commands": []}},
}
