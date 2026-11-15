"""Alexa Smart Home skill Lambda for Alexa Energy Display.

Forwards every directive to Home Assistant's /api/alexa_energy_display/smart_home
with the account-linked access token, and returns Home Assistant's answer. Set the
environment variable BASE_URL to your Home Assistant URL (https://...).
Standard library only; any Python 3.9+ Lambda runtime.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

PATH = "/api/alexa_energy_display/smart_home"
logger = logging.getLogger()
logger.setLevel(logging.DEBUG if os.environ.get("DEBUG") else logging.INFO)


def _token(directive: dict[str, Any]) -> str | None:
    payload = directive.get("payload") or {}
    scope = (
        (directive.get("endpoint") or {}).get("scope")
        or payload.get("grantee")  # Alexa.Authorization AcceptGrant
        or payload.get("scope")  # Alexa.Discovery Discover
        or {}
    )
    return scope.get("token")


def _error(directive: dict[str, Any], kind: str, message: str) -> dict[str, Any]:
    header = directive.get("header") or {}
    event: dict[str, Any] = {
        "header": {
            "namespace": "Alexa",
            "name": "ErrorResponse",
            "payloadVersion": "3",
            "messageId": str(uuid.uuid4()),
        },
        "payload": {"type": kind, "message": message},
    }
    if header.get("correlationToken"):
        event["header"]["correlationToken"] = header["correlationToken"]
    if "endpoint" in directive:
        event["endpoint"] = {"endpointId": directive["endpoint"].get("endpointId")}
    return {"event": event}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    directive = event.get("directive") or {}
    header = directive.get("header") or {}
    logger.info("Directive %s.%s", header.get("namespace"), header.get("name"))
    base = (os.environ.get("BASE_URL") or "").rstrip("/")
    if not base.startswith("https://"):
        return _error(directive, "INTERNAL_ERROR", "BASE_URL is not set to an https:// URL")
    token = _token(directive)
    if not token:
        return _error(directive, "INVALID_AUTHORIZATION_CREDENTIAL", "no access token in the directive")
    request = urllib.request.Request(
        base + PATH,
        data=json.dumps(event).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=7) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        logger.warning("Home Assistant answered HTTP %s", err.code)
        if err.code in (401, 403):
            return _error(directive, "INVALID_AUTHORIZATION_CREDENTIAL", "Home Assistant rejected the token")
        if err.code == 404:
            return _error(directive, "INTERNAL_ERROR", "Alexa Energy Display integration not installed or too old")
        return _error(directive, "INTERNAL_ERROR", f"Home Assistant HTTP {err.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        logger.warning("Home Assistant unreachable: %s", err)
        return _error(directive, "BRIDGE_UNREACHABLE", "Home Assistant is unreachable")
    logger.debug("Response %s", json.dumps(body)[:2000])
    return body
