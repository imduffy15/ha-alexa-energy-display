"""Turn a snapshot into APL directives or speech."""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ask_sdk_model.interfaces.alexa.presentation.apl import (
    ExecuteCommandsDirective,
    OpenUrlCommand,
    RenderDocumentDirective,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "energyDisplay"
REFRESH_MS = 60_000

with open(os.path.join(_HERE, "apl", "fullscreen.json"), encoding="utf-8") as _f:
    FULLSCREEN = json.load(_f)
with open(os.path.join(_HERE, "apl", "empty.json"), encoding="utf-8") as _f:
    EMPTY = json.load(_f)

# rows -> (value, label, sub) font sizes in vh
_SIZES = {1: ("16vh", "5vh", "3.6vh"), 2: ("11vh", "4vh", "3vh"), 3: ("7.5vh", "3.2vh", "2.6vh")}


def layout(count: int) -> dict[str, str]:
    """Columns/rows for a tile count; at most 4 columns x 3 rows."""
    count = max(1, min(count, 12))
    cols = 1 if count == 1 else 2 if count <= 4 else 3 if count <= 9 else 4
    rows = math.ceil(count / cols)
    value, label, sub = _SIZES[min(rows, 3)]
    return {
        "tileWidth": f"{100 / cols:.4f}%",
        "tileHeight": f"{100 / rows:.4f}%",
        "valueSize": value,
        "labelSize": label,
        "subSize": sub,
    }


def fullscreen_payload(snapshot: dict[str, Any] | None, message: str | None = None) -> dict[str, Any]:
    """Data source for apl/fullscreen.json."""
    snapshot = snapshot or {}
    tiles = snapshot.get("tiles") or []
    status = "ok" if tiles and not message else "message"
    return {
        "title": snapshot.get("title") or "Energy",
        "updated": snapshot.get("updated") or "--:--",
        "tiles": tiles,
        "status": status,
        "message": message or "No energy data yet. Check the Alexa Energy Display integration in Home Assistant.",
        "refreshMs": REFRESH_MS,
        **layout(len(tiles)),
    }


def render(snapshot: dict[str, Any] | None, message: str | None = None) -> RenderDocumentDirective:
    return RenderDocumentDirective(
        token=TOKEN,
        document=FULLSCREEN,
        datasources={"payload": fullscreen_payload(snapshot, message)},
    )


def open_url(url: str) -> list:
    """OpenURL needs a rendered document first."""
    return [
        RenderDocumentDirective(token=TOKEN, document=EMPTY),
        ExecuteCommandsDirective(token=TOKEN, commands=[OpenUrlCommand(source=url)]),
    ]
