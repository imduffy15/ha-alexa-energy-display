"""Every field the APL documents bind to is produced by the integration and the skill."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from custom_components.alexa_energy_display.const import DATASTORE_KEY, DATASTORE_NAMESPACE
from custom_components.alexa_energy_display.snapshot import assemble, tile

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lambda"))
import views  # noqa: E402

FULL = ROOT / "lambda/apl/fullscreen.json"
WIDGET = ROOT / "skill-package/dataStorePackages/EnergyWidget/documents/document.json"
TILE_KEYS = set(tile("id", "label", "1", "kW", sub="now", percent=5)) | {"sub", "percent"}


def refs(path: Path, name: str) -> set[str]:
    return set(re.findall(rf"\b{name}\.([A-Za-z_]+)", path.read_text()))


def test_tile_fields_exist():
    for doc in (FULL, WIDGET):
        assert refs(doc, "tile") <= TILE_KEYS, doc


def test_fullscreen_payload_fields_exist():
    payload = views.fullscreen_payload(assemble("Energy", [tile("a", "A", "1", "kW")]))
    assert refs(FULL, "payload") <= set(payload)


def test_widget_datastore_fields_exist():
    snapshot = assemble("Energy", [tile("a", "A", "1", "kW")])
    assert refs(WIDGET, "ds") <= set(snapshot)
    binding = json.loads(WIDGET.read_text())["settings"]["DataStore"]["dataBindings"][0]
    assert (binding["namespace"], binding["key"]) == (DATASTORE_NAMESPACE, DATASTORE_KEY)
    defaults = json.loads((WIDGET.parent.parent / "datasources/default.json").read_text())
    assert refs(WIDGET, "widgetData") <= set(defaults["widgetData"])


def test_manifests_agree():
    skill = json.loads((ROOT / "skill-package/skill.json").read_text())["manifest"]
    interfaces = {i["type"]: i for i in skill["apis"]["custom"]["interfaces"]}
    assert {"ALEXA_PRESENTATION_APL", "ALEXA_DATA_STORE", "ALEXA_DATASTORE_PACKAGEMANAGER", "ALEXA_EXTENSION"} <= set(interfaces)
    packages = {p["id"] for p in interfaces["ALEXA_DATASTORE_PACKAGEMANAGER"]["packages"]}
    pkg_dirs = {p.name for p in (ROOT / "skill-package/dataStorePackages").iterdir()}
    assert packages == pkg_dirs
    for pkg in pkg_dirs:
        manifest = json.loads((ROOT / f"skill-package/dataStorePackages/{pkg}/manifest.json").read_text())
        assert manifest["manifest"]["id"] == pkg
        assert set(manifest["publishingInformation"]["locales"]) == set(skill["publishingInformation"]["locales"])
    models = {p.stem for p in (ROOT / "skill-package/interactionModels/custom").glob("*.json")}
    assert models == set(skill["publishingInformation"]["locales"])
    ext = interfaces["ALEXA_EXTENSION"]["requestedExtensions"]
    assert {"uri": "alexaext:datastore:10"} in ext
