"""Turn a snapshot into APL directives or speech, sized for the requesting device.

Every Alexa device with a screen reports its viewport (pixel size, shape, mode: HUB,
TV, MOBILE, ...) and the highest APL version it renders. The full-screen document is
written to work back to APL 1.3. Rows are built here instead of relying on Container
wrapping, and its version is set to the device's own maximum so older renderers accept it.
"""

from __future__ import annotations

import copy
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
DOCUMENT_VERSION = "2024.1"  # newest features the document uses
MIN_VERSION = "1.3"

with open(os.path.join(_HERE, "apl", "fullscreen.json"), encoding="utf-8") as _f:
    FULLSCREEN = json.load(_f)
with open(os.path.join(_HERE, "apl", "empty.json"), encoding="utf-8") as _f:
    EMPTY = json.load(_f)


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0,)


def document_version(device_max: str | None) -> str:
    """Highest version both the document and the device support."""
    if not device_max:
        return MIN_VERSION
    if _version_key(device_max) >= _version_key(DOCUMENT_VERSION):
        return DOCUMENT_VERSION
    if _version_key(device_max) < _version_key(MIN_VERSION):
        return MIN_VERSION
    return device_max


class Screen:
    """What the device told us about its display."""

    def __init__(self, width: int = 1280, height: int = 800, shape: str = "RECTANGLE", mode: str = "HUB"):
        self.width = max(int(width or 0), 1)
        self.height = max(int(height or 0), 1)
        self.shape = (shape or "RECTANGLE").upper()
        self.mode = (mode or "HUB").upper()

    @property
    def portrait(self) -> bool:
        return self.height > self.width * 1.1

    @property
    def small(self) -> bool:
        return self.shape == "ROUND" or min(self.width, self.height) < 400


def _grid(count: int, screen: Screen) -> tuple[int, int, int]:
    """(tiles shown, columns, rows)."""
    if screen.small:
        shown = min(count, 4)
        cols = 1 if shown == 1 else 2
    elif screen.portrait:
        shown = min(count, 12)
        cols = 1 if shown <= 3 else 2
    else:
        shown = min(count, 12)
        cols = 1 if shown == 1 else 2 if shown <= 4 else 3 if shown <= 9 else 4
    shown = max(shown, 1)
    return shown, cols, math.ceil(shown / cols)


def layout(tiles: list[dict[str, Any]], screen: Screen | None = None) -> dict[str, Any]:
    """Rows of tiles and the sizes that fit them on this screen."""
    screen = screen or Screen()
    shown, cols, rows = _grid(len(tiles), screen)
    # Body height share of the viewport: header and padding take the rest.
    # Round screens keep everything inside the inscribed square (about 71% of the
    # diameter): 15% margins on every side.
    round_ = screen.shape == "ROUND"
    body = 62 if round_ else 84 if not screen.small else 80
    cell_h = body / rows  # in vh
    cell_w = 100 / cols  # in % of the body width
    # Size text from the smaller of a cell's two dimensions, so it fits either way.
    width_vh = (cell_w / 100) * (screen.width / screen.height) * (70 if round_ else 94)
    # Height: value line plus label and sub-line. Width: about 4 characters of value
    # (0.6 em each) plus its unit must fit across the tile.
    value = max(min(cell_h * 0.42, width_vh * 0.2, 18), 3)
    label = max(value * 0.36, 2.2)
    sub = max(value * 0.27, 1.8)
    head = 5.5 if not screen.small else 8
    inner = min(2.5, width_vh * 0.07)
    pad = "15vw" if screen.shape == "ROUND" else "3vw"
    return {
        "rows": [tiles[i : i + cols] for i in range(0, shown, cols)],
        "tileWidth": f"{cell_w:.4f}%",
        "rowHeight": f"{cell_h:.4f}vh",
        "bodyHeight": f"{body}vh",
        "valueSize": f"{value:.2f}vh",
        "labelSize": f"{label:.2f}vh",
        "subSize": f"{sub:.2f}vh",
        "headSize": f"{head}vh",
        "updatedSize": f"{head * 0.6:.2f}vh",
        "sidePadding": pad,
        "innerPad": f"{inner:.2f}vh",
        "cellPad": f"{min(1.0, width_vh * 0.03):.2f}vh",
        "topPadding": "13vh" if round_ else "3vh",
        "bottomPadding": "15vh" if round_ else "3vh",
        "showUpdated": not screen.small,
    }


def fullscreen_data(
    snapshot: dict[str, Any] | None, message: str | None = None, screen: Screen | None = None
) -> dict[str, Any]:
    """Data source for apl/fullscreen.json (bound to the `energy` parameter)."""
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
        **layout(tiles, screen),
    }


def render(
    snapshot: dict[str, Any] | None,
    message: str | None = None,
    screen: Screen | None = None,
    max_version: str | None = None,
) -> RenderDocumentDirective:
    document = copy.deepcopy(FULLSCREEN)
    document["version"] = document_version(max_version)
    return RenderDocumentDirective(
        token=TOKEN,
        document=document,
        datasources={"energy": fullscreen_data(snapshot, message, screen)},
    )


def open_url(url: str, max_version: str | None = None) -> list:
    """OpenURL needs a rendered document first."""
    document = copy.deepcopy(EMPTY)
    document["version"] = document_version(max_version)
    return [
        RenderDocumentDirective(token=TOKEN, document=document),
        ExecuteCommandsDirective(token=TOKEN, commands=[OpenUrlCommand(source=url)]),
    ]
