#!/usr/bin/env python3
"""Create or update the Energy Display Alexa skill end to end with the ASK CLI.

    python3 tools/setup_skill.py --ha-url https://ha.example.com --ha-token "$HA_TOKEN"

Steps (each one is safe to repeat):

 1. Create an Alexa-hosted (Python) skill, or reuse --skill-id.
 2. Read the skill's messaging credentials and configure the Home Assistant
    integration with them (creating the entry if needed, restricted to this skill ID),
    then read the webhook URL back. Or pass --webhook-url and skip Home Assistant.
 3. Import skill-package/ (manifest, interaction models with your invocation name,
    and the EnergyWidget package), keeping the hosted Lambda endpoint.
 4. Push lambda/ plus a generated config.json to the hosted repository and wait for
    Amazon's deployment.
 5. Enable the skill for testing on your account.
 6. Wait for the interaction models to build, simulate "open <invocation>", and check
    that the response carries live tiles and that Home Assistant paired.

Requires: the ASK CLI (`npm install -g ask-cli`) configured with `ask configure`
(no AWS account needed), git, and Python 3.9+. Uses only the standard library.
The widget itself is put on a device from the developer console's widget tool; the
script prints the link.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "skill-package"
LAMBDA = ROOT / "lambda"
DOMAIN = "alexa_energy_display"
REGIONS = {"us-east-1": "US_EAST_1", "eu-west-1": "EU_WEST_1", "us-west-2": "US_WEST_2"}


class SetupError(Exception):
    """A step failed; the message says what to do."""


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def wait(check, what: str, timeout: float = 600, every: float = 5):
    """Call check() until it returns a truthy value."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise SetupError(f"timed out waiting for {what}")
        time.sleep(every)


# --------------------------------------------------------------------------- ASK CLI


class Ask:
    def __init__(self, binary: str, profile: str) -> None:
        self.binary = binary
        self.profile = profile

    def smapi(self, *args: str, full: bool = False, retries: int = 6) -> Any:
        cmd = [self.binary, "smapi", *args, "-p", self.profile]
        if full:
            cmd.append("--full-response")
        for attempt in range(retries):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode == 0 and not out.startswith("[Error]"):
                if not out or out.startswith("Command executed successfully"):
                    return None
                start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
                return json.loads(out[start:]) if start >= 0 else out
            text = out + err
            if any(code in text for code in ("status code 5", "status code 429", "status code 401")) and attempt < retries - 1:
                time.sleep(min(5 * (attempt + 1), 20))  # SMAPI 5xx, throttling, or a token mid-refresh
                continue
            raise SetupError(f"ask smapi {args[0]} failed: {text[:800]}")
        return None

    def vendor_id(self) -> str:
        vendors = self.smapi("get-vendor-list")["vendors"]
        if not vendors:
            raise SetupError("this Amazon account has no developer vendor; open the developer console once")
        return vendors[0]["id"]


# --------------------------------------------------------------------------- skill


def create_hosted_skill(ask: Ask, vendor: str, name: str, locales: list[str], region: str) -> str:
    manifest = {
        "manifestVersion": "1.0",
        "publishingInformation": {
            "locales": {loc: {"name": name, "summary": name, "description": name} for loc in locales},
            "category": "SMART_HOME",
            "isAvailableWorldwide": True,
        },
        "apis": {"custom": {}},
    }
    body = {
        "vendorId": vendor,
        "manifest": {"manifest": manifest},
        "hosting": {"alexaHosted": {"runtime": "PYTHON_3_8", "region": region}},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body, f)
    try:
        created = ask.smapi("create-skill-for-vendor", "--manifest", f"file:{f.name}")
    finally:
        os.unlink(f.name)
    skill_id = created["skillId"]
    log(f"created {skill_id}; waiting for Amazon to provision hosting (about a minute)")

    def ready():
        try:
            meta = ask.smapi("get-alexa-hosted-skill-metadata", "-s", skill_id, retries=1)
        except SetupError:
            return None
        return meta if (meta or {}).get("alexaHosted", {}).get("repository", {}).get("url") else None

    wait(ready, "hosted skill provisioning", timeout=600, every=10)
    return skill_id


def live_endpoint(ask: Ask, skill_id: str) -> dict[str, Any]:
    def has_endpoint():
        manifest = ask.smapi("get-skill-manifest", "-s", skill_id, "-g", "development")["manifest"]
        custom = manifest.get("apis", {}).get("custom", {})
        return custom if custom.get("endpoint") else None

    custom = wait(has_endpoint, "the hosted Lambda endpoint", timeout=300, every=10)
    return {k: custom[k] for k in ("endpoint", "regions") if k in custom}


def build_package(locales: list[str], invocation: str, name: str, endpoint: dict[str, Any]) -> bytes:
    """skill-package/ restricted to the chosen locales, zipped as Amazon exports it."""
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "skill-package"
        shutil.copytree(PACKAGE, pkg)
        skill = json.loads((pkg / "skill.json").read_text())
        m = skill["manifest"]
        repo_locales = set(m["publishingInformation"]["locales"])
        missing = set(locales) - repo_locales
        if missing:
            raise SetupError(f"no interaction model for {sorted(missing)}; available: {sorted(repo_locales)}")
        for section in (m["publishingInformation"], m["privacyAndCompliance"]):
            section["locales"] = {k: v for k, v in section["locales"].items() if k in locales}
        for loc in m["publishingInformation"]["locales"].values():
            loc["name"] = name
            loc["examplePhrases"] = [p.replace("energy display", invocation) for p in loc["examplePhrases"]]
        m["apis"]["custom"].update(endpoint)
        (pkg / "skill.json").write_text(json.dumps(skill, indent=2))
        for model in (pkg / "interactionModels/custom").glob("*.json"):
            if model.stem not in locales:
                model.unlink()
                continue
            data = json.loads(model.read_text())
            data["interactionModel"]["languageModel"]["invocationName"] = invocation
            model.write_text(json.dumps(data, indent=2))
        for widget in (pkg / "dataStorePackages").glob("*/manifest.json"):
            data = json.loads(widget.read_text())
            pub = data["publishingInformation"]["locales"]
            data["publishingInformation"]["locales"] = {k: v for k, v in pub.items() if k in locales}
            widget.write_text(json.dumps(data, indent=2))
        out = Path(tmp) / "package.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(pkg.rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(tmp).as_posix())
        return out.read_bytes()


def import_package(ask: Ask, skill_id: str, package: bytes, attempts: int = 3) -> None:
    """Upload and import. Imports fail transiently now and then, and Amazon answers 409
    while an earlier import of the same skill is still running, so wait and retry."""
    deadline = time.monotonic() + 1800
    attempt = 0
    while True:
        attempt += 1
        try:
            _import_once(ask, skill_id, package)
            return
        except SetupError as err:
            busy = "status code 409" in str(err)
            if time.monotonic() > deadline or (not busy and attempt >= attempts):
                raise
            delay = 60 if busy else 20
            reason = "another import of this skill is still running" if busy else str(err)[:200]
            log(f"import attempt {attempt} failed ({reason}); retrying in {delay} s")
            time.sleep(delay)


def _import_once(ask: Ask, skill_id: str, package: bytes) -> None:
    upload = ask.smapi("create-upload-url")
    request = urllib.request.Request(upload["uploadUrl"], data=package, method="PUT")
    with urllib.request.urlopen(request, timeout=120) as resp:
        if resp.status not in (200, 204):
            raise SetupError(f"package upload failed: HTTP {resp.status}")
    response = ask.smapi("import-skill-package", "--location", upload["uploadUrl"], "-s", skill_id, full=True)
    headers = {k.lower(): v for k, v in (response.get("headers") or [{}])[0].items()} if isinstance(
        response.get("headers"), list
    ) else {k.lower(): v for k, v in (response.get("headers") or {}).items()}
    location = headers.get("location") or ""
    import_id = location.rstrip("/").rsplit("/", 1)[-1]
    if not import_id:
        raise SetupError(f"import did not return an import ID: {json.dumps(response)[:400]}")

    def done():
        status = ask.smapi("get-import-status", "--import-id", import_id)
        return status if status.get("status") != "IN_PROGRESS" else None

    status = wait(done, "the skill package import", timeout=900, every=8)
    if status.get("status") != "SUCCEEDED":
        details = [
            {k: r.get(k) for k in ("name", "status", "errors", "info") if r.get(k)}
            for r in status.get("skill", {}).get("resources", [])
            if r.get("status") not in ("SUCCEEDED", "SKIPPED")
        ]
        raise SetupError(f"package import {status.get('status')}: {json.dumps(details or status)[:1500]}")


def git_credentials(ask: Ask, skill_id: str, repo: str) -> str:
    creds = ask.smapi(
        "generate-credentials-for-alexa-hosted-skill",
        "-s", skill_id, "--repository-url", repo, "--repository-type", "GIT",
    )["repositoryCredentials"]
    user = urllib.parse.quote(creds["username"], safe="")
    password = urllib.parse.quote(creds["password"], safe="")
    return repo.replace("https://", f"https://{user}:{password}@", 1)


def push_code(ask: Ask, skill_id: str, webhook_url: str, timeout_seconds: int) -> str:
    repo = ask.smapi("get-alexa-hosted-skill-metadata", "-s", skill_id)["alexaHosted"]["repository"]["url"]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "hosted"

        def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            proc = subprocess.run(["git", "-C", str(work), *args], capture_output=True, text=True)
            if check and proc.returncode != 0:
                safe = (proc.stderr or proc.stdout).replace(urllib.parse.urlsplit(repo).netloc, "<repo>")
                raise SetupError(f"git {args[0]} failed: {safe[:500]}")
            return proc

        url = git_credentials(ask, skill_id, repo)
        clone = subprocess.run(["git", "clone", "-q", url, str(work)], capture_output=True, text=True)
        if clone.returncode != 0:
            raise SetupError("could not clone the hosted skill repository")
        target = work / "lambda"
        for item in target.iterdir() if target.exists() else []:
            if item.name != "config.json":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(LAMBDA, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "config.json"))
        (target / "config.json").write_text(
            json.dumps({"webhook_url": webhook_url, "timeout_seconds": timeout_seconds}, indent=2) + "\n"
        )
        git("add", "-A")
        if not git("status", "--porcelain").stdout.strip():
            head = git("rev-parse", "HEAD").stdout.strip()
            log("hosted code already up to date")
            return head
        git("-c", "user.name=Energy Display setup", "-c", "user.email=setup@localhost",
            "commit", "-q", "-m", "Energy Display: deploy from setup_skill.py")
        head = git("rev-parse", "HEAD").stdout.strip()
        git("push", "-q", git_credentials(ask, skill_id, repo), "HEAD:master")

    def deployed():
        status = ask.smapi("get-skill-status", "-s", skill_id, "--resource", "hostedSkillDeployment")
        last = status["hostedSkillDeployment"]["lastUpdateRequest"]
        commit = last.get("deploymentDetails", {}).get("commitId", "")
        if commit != head or last["status"] == "IN_PROGRESS":
            return None
        if last["status"] != "SUCCEEDED":
            raise SetupError(f"deployment failed: {json.dumps(last.get('errors'))[:800]}")
        return True

    log(f"pushed {head[:7]}; waiting for the hosted deployment")
    wait(deployed, "the hosted deployment", timeout=900, every=8)
    return head


def wait_for_models(ask: Ask, skill_id: str, locales: list[str]) -> None:
    def built():
        status = ask.smapi("get-skill-status", "-s", skill_id, "--resource", "interactionModel")
        models = status.get("interactionModel", {})
        states = {loc: models.get(loc, {}).get("lastUpdateRequest", {}) for loc in locales}
        for loc, state in states.items():
            if state.get("status") == "FAILED":
                raise SetupError(f"interaction model build failed for {loc}: {json.dumps(state.get('errors'))[:800]}")
        return all(s.get("status") == "SUCCEEDED" for s in states.values())

    wait(built, "the interaction model builds", timeout=900, every=10)


def simulate(ask: Ask, skill_id: str, locale: str, invocation: str) -> dict[str, Any]:
    sim = ask.smapi(
        "simulate-skill", "-s", skill_id, "-g", "development",
        "--input-content", f"open {invocation}", "--device-locale", locale,
        # The full-screen view keeps its session open for refreshes; without a new
        # session the simulator would just end the previous one.
        "--session-mode", "FORCE_NEW_SESSION",
    )

    def finished():
        try:
            result = ask.smapi("get-skill-simulation", "-s", skill_id, "-g", "development", "-i", sim["id"], retries=1)
        except SetupError:
            return None
        return result if result.get("status") != "IN_PROGRESS" else None

    result = wait(finished, "the simulation", timeout=120, every=3)
    if result["status"] != "SUCCESSFUL":
        raise SetupError(f"simulation {result['status']}: {json.dumps(result.get('result', {}).get('error'))}")
    for invocation_info in result["result"]["skillExecutionInfo"]["invocations"]:
        if "invocationResponse" not in invocation_info:
            continue  # e.g. SessionEndedRequest for an earlier session
        response = invocation_info["invocationResponse"]["body"].get("response", {})
        for directive in response.get("directives", []):
            energy = (directive.get("datasources") or {}).get("energy")
            if energy is not None:
                return energy
        speech = (response.get("outputSpeech") or {}).get("ssml")
        raise SetupError(f"the skill answered without a display: {speech}")
    raise SetupError("the simulation did not reach the skill; check the invocation name")


# --------------------------------------------------------------------------- Home Assistant


class HomeAssistant:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as err:
            raise SetupError(f"Home Assistant {method} {path}: HTTP {err.code} {err.read().decode()[:300]}") from err
        except urllib.error.URLError as err:
            raise SetupError(f"Home Assistant unreachable: {err}") from err
        return json.loads(text) if text else None

    def entries(self) -> list[dict[str, Any]]:
        return [e for e in self.call("GET", "/api/config/config_entries/entry") if e["domain"] == DOMAIN]

    def create(self, title: str, client_id: str, client_secret: str, dashboard_url: str | None) -> str:
        flow = self.call("POST", "/api/config/config_entries/flow", {"handler": DOMAIN})
        if flow.get("type") == "abort":
            raise SetupError(f"Home Assistant refused: {flow.get('reason')}. Is the integration installed?")
        step = self.call(
            "POST", f"/api/config/config_entries/flow/{flow['flow_id']}",
            {"title": title, "client_id": client_id, "client_secret": client_secret},
        )
        if step.get("errors"):
            raise SetupError(f"Home Assistant rejected the credentials: {step['errors']}")
        sources = {"use_energy_dashboard": True, "entities": [], "entities_first": False, "update_interval": 60}
        if dashboard_url:
            sources["dashboard_url"] = dashboard_url
        done = self.call("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", sources)
        if done.get("type") != "create_entry":
            raise SetupError(f"Home Assistant setup did not finish: {json.dumps(done)[:300]}")
        return done["result"]["entry_id"]

    def update_options(self, entry_id: str, changes: dict[str, Any]) -> str:
        """Apply changes through the options flow; return the webhook URL it shows."""
        form = self.call("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
        current: dict[str, Any] = {}
        for field in form.get("data_schema", []):
            if "default" in field:
                current[field["name"]] = field["default"]
            suggested = (field.get("description") or {}).get("suggested_value")
            if suggested not in (None, ""):
                current[field["name"]] = suggested
        webhook_url = form.get("description_placeholders", {}).get("webhook_url", "")
        submitted = {**current, **changes, "forget_accounts": False}
        result = self.call("POST", f"/api/config/config_entries/options/flow/{form['flow_id']}", submitted)
        if result.get("type") != "create_entry":
            raise SetupError(f"Home Assistant options not saved: {json.dumps(result.get('errors') or result)[:300]}")
        if not webhook_url.startswith("https://"):
            raise SetupError(
                "Home Assistant has no internet-reachable URL for the webhook. Set Settings → System → "
                "Network → Home Assistant URL (Internet), or sign in to Home Assistant Cloud."
            )
        return webhook_url

    def widget_status(self, entry_title: str) -> dict[str, Any] | None:
        for state in self.call("GET", "/api/states"):
            entity_id = state["entity_id"]
            if (
                entity_id.startswith("sensor.")
                and entity_id.endswith("_widget_status")
                and state["attributes"].get("friendly_name", "").startswith(entry_title)
            ):
                return state
        return None


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skill-id", help="update this skill instead of creating one")
    parser.add_argument("--name", default="Energy Display", help="skill name shown in the Alexa app")
    parser.add_argument("--invocation", default="energy display", help='what you say after "Alexa, open"')
    parser.add_argument(
        "--locale", action="append", dest="locales",
        help="skill locale, repeatable (default en-GB). Must match your Echo's language; "
        "English (Ireland) devices use en-GB",
    )
    parser.add_argument("--region", default="eu-west-1", choices=sorted(REGIONS), help="Alexa-hosted region")
    parser.add_argument("--webhook-url", help="use this webhook URL and skip Home Assistant configuration")
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL"), help="Home Assistant URL (or HA_URL)")
    parser.add_argument("--ha-token", default=os.environ.get("HA_TOKEN"), help="long-lived access token (or HA_TOKEN)")
    parser.add_argument("--ha-title", default="Energy", help="title of the integration entry to create or update")
    parser.add_argument("--dashboard-url", help='opened by "ask energy display to open the dashboard"')
    parser.add_argument("--profile", default="default", help="ASK CLI profile")
    parser.add_argument("--ask", default=shutil.which("ask") or "ask", help="path to the ask binary")
    parser.add_argument("--skip-verify", action="store_true", help="do not simulate a launch at the end")
    args = parser.parse_args()
    locales = args.locales or ["en-GB"]
    invocation = args.invocation.strip().lower()
    if len(invocation.split()) < 2:
        parser.error("the invocation name needs at least two words")
    if not args.webhook_url and not (args.ha_url and args.ha_token):
        parser.error("give --webhook-url, or --ha-url and --ha-token (HA_URL / HA_TOKEN)")

    ask = Ask(args.ask, args.profile)
    try:
        vendor = ask.vendor_id()
        if args.skill_id:
            skill_id = args.skill_id
            log(f"updating {skill_id}")
        else:
            log(f'creating Alexa-hosted skill "{args.name}" ({", ".join(locales)}, {args.region})')
            skill_id = create_hosted_skill(ask, vendor, args.name, locales, REGIONS[args.region])

        webhook_url = args.webhook_url
        ha = HomeAssistant(args.ha_url, args.ha_token) if args.ha_url and args.ha_token else None
        if ha and not webhook_url:
            creds = ask.smapi("get-skill-credentials", "-s", skill_id)["skillMessagingCredentials"]
            entries = [e for e in ha.entries() if e["title"] == args.ha_title] or ha.entries()
            if entries:
                entry_id = entries[0]["entry_id"]
                log(f'updating Home Assistant entry "{entries[0]["title"]}" with this skill\'s credentials')
            else:
                log(f'creating Home Assistant entry "{args.ha_title}"')
                entry_id = ha.create(args.ha_title, creds["clientId"], creds["clientSecret"], args.dashboard_url)
            changes = {"client_id": creds["clientId"], "client_secret": creds["clientSecret"], "skill_id": skill_id}
            if args.dashboard_url:
                changes["dashboard_url"] = args.dashboard_url
            webhook_url = ha.update_options(entry_id, changes)
            log("Home Assistant configured; webhook URL read back (kept out of this output)")

        log("importing skill-package/ (manifest, interaction models, widget)")
        import_package(ask, skill_id, build_package(locales, invocation, args.name, live_endpoint(ask, skill_id)))

        log("deploying lambda/ with config.json")
        push_code(ask, skill_id, webhook_url, 6)

        log("enabling the skill for testing on your account")
        ask.smapi("set-skill-enablement", "-s", skill_id, "-g", "development")

        log("waiting for the interaction models to build")
        wait_for_models(ask, skill_id, locales)

        if not args.skip_verify:
            log(f'simulating "open {invocation}" ({locales[0]})')
            energy = simulate(ask, skill_id, locales[0], invocation)
            tiles = sum(len(r) for r in energy.get("rows", []))
            if energy.get("status") != "ok":
                raise SetupError(f"the skill reached Home Assistant but shows: {energy.get('message')}")
            log(f"launch OK: {tiles} tiles, updated {energy.get('updated')}")
            if ha:
                state = wait(lambda: ha.widget_status(args.ha_title), "the widget status sensor", 30, 3)
                log(f"Home Assistant widget status: {state['state']} "
                    f"({state['attributes'].get('paired_accounts')} paired account(s))")
    except SetupError as err:
        print(f"\nerror: {err}", file=sys.stderr)
        return 1

    console = f"https://developer.amazon.com/alexa/console/ask/build/custom/{skill_id}/development/{locales[0].replace('-', '_')}/dashboard"
    print(
        f"""
Done. Skill ID: {skill_id}

On your Echo, say "Alexa, open {invocation}".
Widget: open the developer console, Build → Multimodal → Widgets → Home Energy → Install,
and pick your Echo Show:
  {console}
"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
