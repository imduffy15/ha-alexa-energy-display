"""Call the Home Assistant webhook registered by the Alexa Energy Display integration."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))


class HomeAssistantError(Exception):
    """Home Assistant could not be reached or refused the request."""


class NotConfigured(HomeAssistantError):
    """No webhook URL is configured."""


def load_config() -> dict[str, Any]:
    """Read config.json beside this file; WEBHOOK_URL in the environment wins."""
    config: dict[str, Any] = {}
    path = os.path.join(_HERE, "config.json")
    if not os.path.exists(path) and not os.environ.get("WEBHOOK_URL"):
        raise NotConfigured(f"{path} does not exist; files here: {sorted(os.listdir(_HERE))}")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    if os.environ.get("WEBHOOK_URL"):
        config["webhook_url"] = os.environ["WEBHOOK_URL"]
    url = (config.get("webhook_url") or "").strip()
    if not url.startswith("https://") or "PASTE-FROM-HOME-ASSISTANT" in url:
        raise NotConfigured("webhook_url is missing from lambda/config.json")
    config["webhook_url"] = url
    return config


def call(event_type: str, identity: dict[str, Any], detail: Any = None) -> dict[str, Any]:
    """POST one event and return the decoded response."""
    config = load_config()
    body = {"type": event_type, **identity}
    if detail is not None:
        body["detail"] = detail
    request = urllib.request.Request(
        config["webhook_url"],
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "alexa-energy-display"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(config.get("timeout_seconds", 6))) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        raise HomeAssistantError(f"Home Assistant answered HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        raise HomeAssistantError(f"Home Assistant unreachable: {err}") from err
