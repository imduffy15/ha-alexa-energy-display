"""Render mock-ups of the widget and full-screen view, plus icons.

The pictures follow the same layout rules as the APL documents (tile grid from
lambda/views.py, sizes in vw/vh), but are drawn by Chromium from HTML, not by an Echo.
They are illustrations for the README and the widget gallery listing.

    python tools/render_previews.py --chrome /path/to/chrome-headless-shell
"""

from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lambda"))

import views  # noqa: E402

from custom_components.alexa_energy_display.snapshot import COLORS, assemble, tile  # noqa: E402

SAMPLE = [
    tile("solar_now", "Solar", "3.42", "kW", COLORS["solar"], "now"),
    tile("grid_now", "Grid export", "1.18", "kW", COLORS["export"], "now"),
    tile("battery", "Battery", "76", "%", COLORS["battery"], "charging 0.95 kW", 76),
    tile("home_now", "Home", "1.29", "kW", COLORS["home"], "now"),
    tile("solar_today", "Solar", "14.8", "kWh", COLORS["solar"], "today"),
    tile("import_today", "Imported", "2.31", "kWh", COLORS["import"], "today"),
    tile("export_today", "Exported", "6.07", "kWh", COLORS["export"], "today"),
    tile("home_today", "Home used", "9.62", "kWh", COLORS["home"], "today"),
    tile("sensor.tariff", "Tariff", "0.23", "€/kWh", COLORS["monetary"]),
]

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0B1220; font-family: 'DejaVu Sans', sans-serif; overflow: hidden; }
.root { width: 100vw; height: 100vh; display: flex; flex-direction: column; }
.head { display: flex; justify-content: space-between; align-items: center; }
.title { color: #fff; font-weight: bold; white-space: nowrap; }
.upd { color: #7F8EA6; white-space: nowrap; }
.grid { display: flex; flex-wrap: wrap; align-content: flex-start; }
.cell { display: flex; }
.card { background: #141D2E; width: 100%; height: 100%; display: flex; overflow: hidden; }
.accent { height: 100%; }
.body { flex: 1; display: flex; flex-direction: column; justify-content: center; min-width: 0; }
.label { color: #A9B7CC; white-space: nowrap; }
.row { display: flex; align-items: baseline; }
.value { font-weight: bold; white-space: nowrap; }
.unit { color: #DDE5F0; white-space: nowrap; }
.sub { color: #7F8EA6; white-space: nowrap; }
.track { background: #24304A; width: 100%; }
.fill { height: 100%; }
"""


def tile_html(t, *, value, label, unit, sub, accent, radius, pad, padl, padr, bar, barpad, sub_when_bar):
    color = t["color"]
    parts = [f'<div class="label" style="font-size:{label}">{html.escape(t["label"])}</div>']
    parts.append(
        f'<div class="row"><span class="value" style="font-size:{value};color:{color}">{html.escape(t["value"])}</span>'
        f'<span class="unit" style="font-size:{unit};padding-left:1vh">{html.escape(t["unit"])}</span></div>'
    )
    if t.get("sub") and (sub_when_bar or t.get("percent") is None):
        parts.append(f'<div class="sub" style="font-size:{sub}">{html.escape(t["sub"])}</div>')
    if t.get("percent") is not None:
        parts.append(
            f'<div style="padding-top:{barpad}"><div class="track" style="height:{bar};border-radius:1vh">'
            f'<div class="fill" style="width:{t["percent"]}%;background:{color};border-radius:1vh"></div></div></div>'
        )
    accent_html = f'<div class="accent" style="width:{accent};background:{color};border-radius:{radius}"></div>' if accent else ""
    return (
        f'<div class="card" style="border-radius:{radius}">{accent_html}'
        f'<div class="body" style="padding-left:{padl};padding-right:{padr}">{"".join(parts)}</div></div>'
    )


def fullscreen_html(snapshot, screen=None):
    p = views.fullscreen_data(snapshot, screen=screen)
    rows = "".join(
        f'<div style="display:flex;width:100%;height:{p["rowHeight"]}">'
        + "".join(
            f'<div class="cell" style="width:{p["tileWidth"]};height:100%;padding:{p["cellPad"]}">'
            + tile_html(t, value=p["valueSize"], label=p["labelSize"], unit=p["labelSize"], sub=p["subSize"],
                        accent="0.8vh", radius="2vh", pad=p["cellPad"], padl=p["innerPad"], padr=p["innerPad"], bar="1.2vh", barpad="1vh",
                        sub_when_bar=True)
            + "</div>"
            for t in row
        )
        + "</div>"
        for row in p["rows"]
    )
    updated = f'<div class="upd" style="font-size:{p["updatedSize"]}">Updated {p["updated"]}</div>' if p["showUpdated"] else ""
    return (
        f'<div class="root" style="padding:{p["topPadding"]} {p["sidePadding"]} {p["bottomPadding"]}">'
        f'<div class="head" style="height:10vh;padding:0 1vh"><div class="title" style="font-size:{p["headSize"]}">{html.escape(p["title"])}</div>'
        f'{updated}</div>'
        f'<div style="height:{p["bodyHeight"]}">{rows}</div></div>'
    )


def widget_html(snapshot):
    cells = "".join(
        '<div class="cell" style="width:50%;height:50%;padding:1.5vw">'
        + tile_html(t, value="11vw", label="6vw", unit="5.5vw", sub="4.5vw", accent=None, radius="4vw",
                    pad="1.5vw", padl="4vw", padr="3vw", bar="2vw", barpad="1.5vw", sub_when_bar=False)
        + "</div>"
        for t in snapshot["widget"]
    )
    return (
        f'<div class="root" style="padding:3vw">'
        f'<div class="head" style="height:14vh;padding:0 1.5vw"><div class="title" style="font-size:7vw">{html.escape(snapshot["title"])}</div>'
        f'<div class="upd" style="font-size:5vw">{snapshot["updated"]}</div></div>'
        f'<div class="grid" style="height:80vh">{cells}</div></div>'
    )


def icon_html():
    bolt = (
        '<svg viewBox="0 0 100 100" width="100%" height="100%"><rect width="100" height="100" rx="22" fill="#0B1220"/>'
        '<circle cx="50" cy="50" r="34" fill="none" stroke="#141D2E" stroke-width="10"/>'
        '<path d="M50 16 A34 34 0 0 1 84 50" fill="none" stroke="#F5B700" stroke-width="10" stroke-linecap="round"/>'
        '<path d="M84 50 A34 34 0 0 1 50 84" fill="none" stroke="#3DD68C" stroke-width="10" stroke-linecap="round"/>'
        '<path d="M50 84 A34 34 0 0 1 22 66" fill="none" stroke="#4DA3FF" stroke-width="10" stroke-linecap="round"/>'
        '<path d="M55 28 L38 54 H50 L45 72 L63 45 H51 Z" fill="#FFFFFF"/></svg>'
    )
    return f'<div style="width:100vw;height:100vh">{bolt}</div>'


def shoot(chrome: str, body: str, out: Path, w: int, h: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(f"<!doctype html><meta charset=utf-8><style>{CSS}</style>{body}", encoding="utf-8")
        subprocess.run(
            [chrome, "--headless", "--no-sandbox", "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--window-size={w},{h}", f"--screenshot={out}", page.as_uri()],
            check=True, capture_output=True,
        )
    print(f"wrote {out.relative_to(ROOT)} ({w}x{h})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    args = parser.parse_args()
    snapshot = assemble("Energy", SAMPLE, datetime(2026, 11, 15, 14, 5))
    assets = ROOT / "skill-package/assets"
    docs = ROOT / "docs/images"
    assets.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
    shoot(args.chrome, widget_html(snapshot), assets / "widget-preview-328x552.png", 328, 552)
    shoot(args.chrome, icon_html(), assets / "widget-icon-450.png", 450, 450)
    shoot(args.chrome, icon_html(), assets / "icon-512.png", 512, 512)
    shoot(args.chrome, icon_html(), assets / "icon-108.png", 108, 108)
    devices = [
        ("echo-show-8", 1280, 800, "RECTANGLE", "HUB"),
        ("echo-show-5", 960, 480, "RECTANGLE", "HUB"),
        ("echo-show-15-portrait", 1080, 1920, "RECTANGLE", "HUB"),
        ("echo-spot-round", 480, 480, "ROUND", "HUB"),
        ("echo-spot-2024", 320, 240, "RECTANGLE", "HUB"),
        ("fire-tv", 1920, 1080, "RECTANGLE", "TV"),
    ]
    for name, w, h, shape, mode in devices:
        shoot(args.chrome, fullscreen_html(snapshot, views.Screen(w, h, shape, mode)), docs / f"fullscreen-{name}.png", w, h)
    four = assemble("Energy", SAMPLE[:4], datetime(2026, 11, 15, 14, 5))
    shoot(args.chrome, fullscreen_html(four), docs / "fullscreen-4-tiles.png", 1280, 800)


if __name__ == "__main__":
    main()
