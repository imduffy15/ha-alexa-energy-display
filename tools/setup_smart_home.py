#!/usr/bin/env python3
"""Create or update the Energy Display Voice smart home skill end to end.

    python3 tools/setup_smart_home.py --ha-url https://ha.example.com --ha-token "$HA_TOKEN" \\
        --aws-profile my-profile --region eu-west-1

Smart home skills cannot be Alexa-hosted, so this deploys a small Lambda
(smart-home/lambda/) into your AWS account. Steps, each safe to repeat:

 1. Create or update the Lambda `energy-display-smart-home` (Python 3.12, standard library
    only) with BASE_URL set to your Home Assistant, and its execution role.
 2. Create the smart home skill (or update --skill-id) with that Lambda as its endpoint,
    and restrict the Lambda's Alexa trigger to this skill ID.
 3. Configure account linking to Home Assistant's own OAuth (/auth/authorize, /auth/token),
    with the Amazon client ID for your region, as Home Assistant documents.
 4. Put the skill's messaging credentials (for proactive reports) and an optional voice
    name prefix into the Alexa Energy Display integration.
 5. Check Home Assistant's smart home endpoint with a real Discover directive.

Then link the skill in the Alexa app (you sign in to Home Assistant there) and say
"Alexa, discover devices". The script prints those steps.

Requires: the ASK CLI (`ask configure`), the AWS CLI with credentials for --aws-profile,
and the Alexa Energy Display integration 0.2.0+ in Home Assistant.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_skill import Ask, HomeAssistant, SetupError, log, wait  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_SRC = ROOT / "smart-home/lambda/lambda_function.py"
MANIFEST = ROOT / "smart-home/skill-package/skill.json"
SMART_HOME_PATH = "/api/alexa_energy_display/smart_home"
ROLE_NAME = "energy-display-smart-home"
# Lambda region -> Alexa account-linking client ID (Home Assistant's documented values).
CLIENT_IDS = {
    "eu-west-1": "https://layla.amazon.com/",
    "us-east-1": "https://pitangui.amazon.com/",
    "us-west-2": "https://alexa.amazon.co.jp/",
}
ALEXA_REGION = {"eu-west-1": "EU", "us-east-1": "NA", "us-west-2": "FE"}


class Aws:
    def __init__(self, profile: str | None, region: str) -> None:
        self.base = ["aws", "--region", region, "--output", "json"] + (["--profile", profile] if profile else [])

    def __call__(self, *args: str, check: bool = True) -> Any:
        proc = subprocess.run([*self.base, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            if check:
                raise SetupError(f"aws {' '.join(args[:2])} failed: {proc.stderr.strip()[:600]}")
            return None
        return json.loads(proc.stdout) if proc.stdout.strip() else {}


def ensure_role(aws: Aws) -> str:
    role = aws("iam", "get-role", "--role-name", ROLE_NAME, check=False)
    if role:
        return role["Role"]["Arn"]
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    role = aws("iam", "create-role", "--role-name", ROLE_NAME, "--assume-role-policy-document", json.dumps(trust),
               "--description", "Alexa Energy Display smart home Lambda")
    aws("iam", "attach-role-policy", "--role-name", ROLE_NAME,
        "--policy-arn", "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    log("created IAM role; waiting for it to propagate")
    time.sleep(12)
    return role["Role"]["Arn"]


def deploy_lambda(aws: Aws, name: str, base_url: str) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(LAMBDA_SRC, "lambda_function.py")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(buf.getvalue())
    env = json.dumps({"Variables": {"BASE_URL": base_url}})
    try:
        existing = aws("lambda", "get-function", "--function-name", name, check=False)
        if existing:
            aws("lambda", "update-function-code", "--function-name", name, "--zip-file", f"fileb://{f.name}")
            aws("lambda", "wait", "function-updated-v2", "--function-name", name)
            aws("lambda", "update-function-configuration", "--function-name", name, "--environment", env,
                "--timeout", "8", "--runtime", "python3.12", "--handler", "lambda_function.lambda_handler")
        else:
            role = ensure_role(aws)
            for attempt in range(5):
                created = aws(
                    "lambda", "create-function", "--function-name", name, "--runtime", "python3.12",
                    "--handler", "lambda_function.lambda_handler", "--role", role, "--timeout", "8",
                    "--memory-size", "128", "--zip-file", f"fileb://{f.name}", "--environment", env,
                    "--description", "Alexa Energy Display smart home forwarder", check=attempt == 4,
                )
                if created:
                    break
                time.sleep(8)  # a new role can take a few seconds before Lambda accepts it
        aws("lambda", "wait", "function-updated-v2", "--function-name", name)
    finally:
        os.unlink(f.name)
    return aws("lambda", "get-function", "--function-name", name)["Configuration"]["FunctionArn"]


def allow_alexa(aws: Aws, name: str, skill_id: str | None) -> None:
    """Alexa trigger: bootstrap without a skill ID, then restricted to the skill."""
    policy = aws("lambda", "get-policy", "--function-name", name, check=False) or {}
    statements = json.loads(policy.get("Policy", "{}")).get("Statement", [])
    sids = {s["Sid"] for s in statements}
    if skill_id is None:
        if "alexa-bootstrap" not in sids:
            aws("lambda", "add-permission", "--function-name", name, "--statement-id", "alexa-bootstrap",
                "--action", "lambda:InvokeFunction", "--principal", "alexa-connectedhome.amazon.com")
        return
    sid = "alexa-" + skill_id.rsplit(".", 1)[-1]
    if sid not in sids:
        aws("lambda", "add-permission", "--function-name", name, "--statement-id", sid,
            "--action", "lambda:InvokeFunction", "--principal", "alexa-connectedhome.amazon.com",
            "--event-source-token", skill_id)
    if "alexa-bootstrap" in sids:
        aws("lambda", "remove-permission", "--function-name", name, "--statement-id", "alexa-bootstrap")


def skill_manifest(name: str, locales: list[str], arn: str, region: str) -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text())
    m = manifest["manifest"]
    for section in (m["publishingInformation"], m["privacyAndCompliance"]):
        section["locales"] = {k: v for k, v in section["locales"].items() if k in locales}
    for loc in m["publishingInformation"]["locales"].values():
        loc["name"] = name
    m["apis"]["smartHome"]["endpoint"] = {"uri": arn}
    m["apis"]["smartHome"]["regions"] = {ALEXA_REGION[region]: {"endpoint": {"uri": arn}}}
    return manifest


def wait_manifest(ask: Ask, skill_id: str) -> None:
    def done():
        status = ask.smapi("get-skill-status", "-s", skill_id, "--resource", "manifest")
        last = status["manifest"]["lastUpdateRequest"]
        if last["status"] == "FAILED":
            raise SetupError(f"manifest update failed: {json.dumps(last.get('errors'))[:800]}")
        return last["status"] == "SUCCEEDED"

    wait(done, "the manifest update", timeout=300, every=5)


def _file(data: dict[str, Any]) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
    return f.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--skill-id", help="update this smart home skill instead of creating one")
    parser.add_argument("--name", default="Energy Display Voice")
    parser.add_argument("--locale", action="append", dest="locales", help="repeatable; default en-GB")
    parser.add_argument("--region", default="eu-west-1", choices=sorted(CLIENT_IDS),
                        help="Lambda region: eu-west-1 (Europe), us-east-1 (Americas), us-west-2 (Far East)")
    parser.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--function-name", default="energy-display-smart-home")
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL"), required=not os.environ.get("HA_URL"))
    parser.add_argument("--ha-token", default=os.environ.get("HA_TOKEN"), help="long-lived token (or HA_TOKEN)")
    parser.add_argument("--ha-title", default="Energy", help="integration entry to configure")
    parser.add_argument("--voice-prefix", help='e.g. "Ballyer" gives "Ballyer Battery"; empty clears it')
    parser.add_argument("--profile", default="default", help="ASK CLI profile")
    parser.add_argument("--ask", default=shutil.which("ask") or "ask")
    args = parser.parse_args()
    locales = args.locales or ["en-GB"]
    ha_url = args.ha_url.rstrip("/")
    if not ha_url.startswith("https://"):
        parser.error("--ha-url must be the https:// URL Alexa can reach")

    ask, aws = Ask(args.ask, args.profile), Aws(args.aws_profile, args.region)
    try:
        ask.vendor_id()
        identity = aws("sts", "get-caller-identity")
        log(f"AWS account {identity['Account']}, region {args.region}")

        log(f"deploying Lambda {args.function_name}")
        arn = deploy_lambda(aws, args.function_name, ha_url)

        if args.skill_id:
            skill_id = args.skill_id
            allow_alexa(aws, args.function_name, skill_id)
            log(f"updating skill {skill_id}")
            ask.smapi("update-skill-manifest", "-s", skill_id, "-g", "development",
                      "--manifest", f"file:{_file(skill_manifest(args.name, locales, arn, args.region))}")
        else:
            allow_alexa(aws, args.function_name, None)
            log(f'creating smart home skill "{args.name}"')
            body = {"vendorId": ask.vendor_id(), "manifest": skill_manifest(args.name, locales, arn, args.region)}
            skill_id = ask.smapi("create-skill-for-vendor", "--manifest", f"file:{_file(body)}")["skillId"]
            allow_alexa(aws, args.function_name, skill_id)
        wait_manifest(ask, skill_id)

        log("configuring account linking to Home Assistant")
        linking = {"accountLinkingRequest": {
            "type": "AUTH_CODE",
            "authorizationUrl": f"{ha_url}/auth/authorize",
            "accessTokenUrl": f"{ha_url}/auth/token",
            "clientId": CLIENT_IDS[args.region],
            "clientSecret": uuid.uuid4().hex,  # Home Assistant does not check it
            "accessTokenScheme": "REQUEST_BODY_CREDENTIALS",
            "scopes": ["smart_home"],
            "domains": [],
        }}
        ask.smapi("update-account-linking-info", "-s", skill_id, "-g", "development",
                  "--account-linking-request", f"file:{_file(linking)}")

        if args.ha_token:
            ha = HomeAssistant(ha_url, args.ha_token)
            entries = [e for e in ha.entries() if e["title"] == args.ha_title] or ha.entries()
            if not entries:
                raise SetupError("no Alexa Energy Display entry in Home Assistant; run setup_skill.py first")
            creds = ask.smapi("get-skill-credentials", "-s", skill_id)["skillMessagingCredentials"]
            changes = {"smart_home_client_id": creds["clientId"], "smart_home_client_secret": creds["clientSecret"]}
            if args.voice_prefix is not None:
                changes["voice_prefix"] = args.voice_prefix
            ha.update_options(entries[0]["entry_id"], changes)
            log(f'configured Home Assistant entry "{entries[0]["title"]}"')
            discover = {"directive": {"header": {"namespace": "Alexa.Discovery", "name": "Discover",
                                                 "payloadVersion": "3", "messageId": str(uuid.uuid4())},
                                      "payload": {"scope": {"type": "BearerToken", "token": args.ha_token}}}}
            out = ha.call("POST", SMART_HOME_PATH, discover)
            names = sorted(e["friendlyName"] for e in out["event"]["payload"]["endpoints"])
            log(f"Home Assistant offers {len(names)} voice devices: {', '.join(names)}")
    except SetupError as err:
        print(f"\nerror: {err}", file=sys.stderr)
        return 1

    print(f"""
Done. Smart home skill ID: {skill_id}

Finish in the Alexa app (this needs your Home Assistant login, so it can't be scripted):
  1. More → Skills & Games → Your Skills → Dev → {args.name} → Enable to use.
  2. Sign in to Home Assistant on the page that opens (Keep me logged in).
  3. Let Alexa discover devices, or say "Alexa, discover devices".
Then ask: "Alexa, what's the battery level?" or use the devices in routines,
e.g. When Battery State becomes Discharging.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
