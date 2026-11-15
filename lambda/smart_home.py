"""Forward Alexa Smart Home directives to Home Assistant.

Used when this Alexa-hosted skill also has the smart home API (a multi-capability
skill), so voice works without a Lambda in your own AWS account. Home Assistant is
the host of `webhook_url` in config.json (or BASE_URL); every directive carries the
account-linked Home Assistant token, which Home Assistant checks.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import ha_client

PATH = "/api/alexa_energy_display/smart_home"
logger = logging.getLogger(__name__)


def base_url() -> str | None:
    if os.environ.get("BASE_URL"):
        return os.environ["BASE_URL"].rstrip("/")
    try:
        url = ha_client.load_config()["webhook_url"]
    except ha_client.HomeAssistantError:
        return None
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _token(directive: dict[str, Any]) -> str | None:
    payload = directive.get("payload") or {}
    scope = (directive.get("endpoint") or {}).get("scope") or payload.get("grantee") or payload.get("scope") or {}
    return scope.get("token")


def error(directive: dict[str, Any], kind: str, message: str) -> dict[str, Any]:
    header = directive.get("header") or {}
    event: dict[str, Any] = {
        "header": {"namespace": "Alexa", "name": "ErrorResponse", "payloadVersion": "3",
                   "messageId": str(uuid.uuid4())},
        "payload": {"type": kind, "message": message},
    }
    if header.get("correlationToken"):
        event["header"]["correlationToken"] = header["correlationToken"]
    if "endpoint" in directive:
        event["endpoint"] = {"endpointId": directive["endpoint"].get("endpointId")}
    return {"event": event}


def forward(event: dict[str, Any]) -> dict[str, Any]:
    directive = event.get("directive") or {}
    header = directive.get("header") or {}
    logger.info("Smart home directive %s.%s", header.get("namespace"), header.get("name"))
    base = base_url()
    if not base or not base.startswith("https://"):
        return error(directive, "INTERNAL_ERROR", "Home Assistant URL is not configured")
    token = _token(directive)
    if not token:
        return error(directive, "INVALID_AUTHORIZATION_CREDENTIAL", "no access token in the directive")
    request = urllib.request.Request(
        base + PATH,
        data=json.dumps(event).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=7) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        logger.warning("Home Assistant answered HTTP %s", err.code)
        if err.code in (401, 403):
            return error(directive, "INVALID_AUTHORIZATION_CREDENTIAL", "Home Assistant rejected the token")
        return error(directive, "INTERNAL_ERROR", f"Home Assistant HTTP {err.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        logger.warning("Home Assistant unreachable: %s", err)
        return error(directive, "BRIDGE_UNREACHABLE", "Home Assistant is unreachable")
