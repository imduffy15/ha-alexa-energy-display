"""Client for the Alexa Data Store REST API."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    DATASTORE_KEY,
    DATASTORE_NAMESPACE,
    DELIVERY_WINDOW_SECONDS,
    LWA_SCOPE,
    LWA_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=20)


class DataStoreError(Exception):
    """A Data Store or Login with Amazon call failed."""


class AuthError(DataStoreError):
    """The skill messaging credentials were rejected."""


@dataclass(slots=True)
class PushResult:
    """Outcome of one commands call."""

    ok: bool
    delivered: int
    queued: bool
    detail: str


class DataStoreClient:
    """Push snapshots to an Echo's on-device data store."""

    def __init__(
        self, session: aiohttp.ClientSession, client_id: str, client_secret: str
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expiry = 0.0

    async def async_get_token(self, force: bool = False) -> str:
        """Return a cached LWA access token, fetching one when needed."""
        if not force and self._token and time.monotonic() < self._token_expiry:
            return self._token
        try:
            async with self._session.post(
                LWA_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": LWA_SCOPE,
                },
                timeout=_TIMEOUT,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status in (400, 401):
                    raise AuthError(
                        (body or {}).get("error_description")
                        or (body or {}).get("error")
                        or f"HTTP {resp.status}"
                    )
                if resp.status != 200 or "access_token" not in (body or {}):
                    raise DataStoreError(f"token request failed: HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise DataStoreError(f"token request failed: {err}") from err
        self._token = body["access_token"]
        self._token_expiry = time.monotonic() + max(
            60, int(body.get("expires_in", 3600)) - 120
        )
        return self._token

    async def async_put_snapshot(
        self, api_endpoint: str, user_id: str, snapshot: dict[str, Any]
    ) -> PushResult:
        """Write the snapshot to every widget-capable device of one Alexa user."""
        until = datetime.now(UTC) + timedelta(seconds=DELIVERY_WINDOW_SECONDS)
        body = {
            "commands": [
                {"type": "PUT_NAMESPACE", "namespace": DATASTORE_NAMESPACE},
                {
                    "type": "PUT_OBJECT",
                    "namespace": DATASTORE_NAMESPACE,
                    "key": DATASTORE_KEY,
                    "content": snapshot,
                },
            ],
            "target": {"type": "USER", "id": user_id},
            "attemptDeliveryUntil": until.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        url = f"{api_endpoint.rstrip('/')}/v1/datastore/commands"
        for attempt in (1, 2):
            token = await self.async_get_token(force=attempt == 2)
            try:
                async with self._session.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_TIMEOUT,
                ) as resp:
                    raw = await resp.text()
                    status = resp.status
                try:
                    data = json.loads(raw) if raw else None
                except ValueError:
                    data = None
            except (aiohttp.ClientError, TimeoutError) as err:
                raise DataStoreError(f"data store request failed: {err}") from err
            if status == 401 and attempt == 1:
                continue
            break
        if status == 401:
            raise AuthError("data store rejected the access token")
        if status != 200:
            message = (data or {}).get("type") or (data or {}).get("message") or ""
            raise DataStoreError(f"data store HTTP {status} {message}".strip())
        results = (data or {}).get("results") or []
        delivered = sum(1 for r in results if r.get("type") == "SUCCESS")
        failures = sorted({r.get("type", "?") for r in results if r.get("type") != "SUCCESS"})
        queued = bool((data or {}).get("queuedResultId"))
        detail = f"{delivered} delivered" + (f", {', '.join(failures)}" if failures else "")
        if queued:
            detail += ", queued for offline devices"
        if not results:
            detail = "accepted, no devices with the widget yet"
        return PushResult(ok=not failures or queued, delivered=delivered, queued=queued, detail=detail)
