# Voice

There are two ways to ask Alexa about your energy.

## 1. Ask the display skill (Alexa-hosted, nothing else to install)

The display skill answers questions itself, on Amazon's hosting:

- "Alexa, ask energy display about the battery" → "Battery is at 78 percent, discharging
  270 watts. Battery in / out today is 9.35 in, 2.25 kilowatt hours."
- "Alexa, ask energy display how's the solar / about the grid / about my usage / about today"
- Extra entities by their name: "Alexa, ask energy display about the tariff"

For shorter phrases, add an Alexa routine: *When you say* "how's the battery" → *Custom
action* "ask energy display about the battery".

## 2. Smart home devices (needs a Lambda in your own AWS account)

The smart home skill below makes Solar, Grid, Battery and Home Energy real Alexa devices,
so you can ask without the skill name and trigger routines on changes. Amazon only
accepts a Lambda with an *Alexa Smart Home* trigger as a smart home endpoint. Alexa-hosted
Lambdas live in Amazon's account, where no one can add that trigger: tried 2026-11-15,
`update-skill-manifest` refused with *Please make sure that "Alexa Smart Home" is selected
for the event source type*. So this part needs your own AWS account, the same way
Home Assistant's own Alexa smart home setup (haaska) does.

### The Energy Display Voice smart home skill

The display skill shows tiles. This second, optional skill puts your energy data into
Alexa's smart home as **read-only devices**, so you can ask without an invocation
name and use the values in routines:

| Device | Values Alexa can report | Changes Alexa is told about |
| --- | --- | --- |
| **Solar** | Power (W), Energy today (kWh) | |
| **Grid** | Power (W, positive = importing), Import today, Export today (kWh), Direction (importing / exporting / idle) | Direction |
| **Battery** | Level (%), Power (W, positive = discharging), State (charging / discharging / idle) | Level, State |
| **Home Energy** | Power (W), Energy today (kWh) | |
| *each extra entity* | Its value; temperatures appear as temperature sensors | Percentages |

Nothing can be switched or set: every control is marked non-controllable. Names get
the optional *voice prefix*, for example "Ballyer Battery", so two houses on one
Amazon account stay distinct.

Things to try:

- "Alexa, what's the battery level?" / "Alexa, what's the temperature of hot water?"
- Alexa app → device → see the current values.
- Routines: *When* Battery **State** becomes *Discharging*, or Grid **Direction** becomes
  *Exporting* → announce, run a scene, and so on. Which device states the Alexa app
  offers as routine triggers is Amazon's choice. Mode changes are the most widely
  offered, so they and the battery level are the ones reported proactively.

Amazon decides which phrasings work for range values. "What's the battery level" and
temperatures work best. If one doesn't, give it a routine with your own phrase.

## How it works

```
Echo ─► Alexa smart home ─► your Lambda (smart-home/lambda) ─► HA /api/alexa_energy_display/smart_home
                                                               (authenticated with the linked HA token)
HA ──► Alexa event gateway: ChangeReports for battery level/state and grid direction
```

- **Account linking** uses Home Assistant's own OAuth (`/auth/authorize`,
  `/auth/token`), as for Home Assistant's built-in Alexa integration. You sign in to
  Home Assistant once in the Alexa app.
- The Lambda only forwards directives with the linked token. Home Assistant
  authenticates each call. No Home Assistant token is stored in AWS.
- Values come from the same snapshot as the display (Energy dashboard + extra entities),
  refreshed on the integration's update interval.
- Proactive ChangeReports need the skill's messaging credentials in the integration. Without
  them, Alexa still answers questions but routines can't trigger on changes.

## Automatic setup

Needs the ASK CLI (`ask configure`), the AWS CLI with credentials (for example
`aws sso login --profile <profile>`), and the integration 0.2.0 or newer.

```sh
export HA_URL=https://ha.example.com HA_TOKEN=<long-lived access token>
python3 tools/setup_smart_home.py --aws-profile <profile> --region eu-west-1
# second house on the same Amazon account:
python3 tools/setup_smart_home.py --ha-url https://other.example.com --ha-token ... \
    --ha-title "Other Energy" --voice-prefix Other --function-name energy-display-other
```

`--region` picks both the Lambda region and the account-linking client ID:
`eu-west-1` for Europe (including the UK and Ireland), `us-east-1` for the Americas,
`us-west-2` for Japan and Australia. The script:

1. Creates or updates the Lambda `energy-display-smart-home` and its role, with
   `BASE_URL` set to your Home Assistant.
2. Creates the smart home skill (or updates `--skill-id`), pointing at the Lambda,
   and restricts the Lambda's Alexa trigger to that skill.
3. Configures account linking to Home Assistant.
4. Stores the skill's messaging credentials and voice prefix in the integration.
5. Sends a real Discover directive to Home Assistant and lists the devices it would offer.

Then, in the Alexa app: **More → Skills & Games → Your Skills → Dev → Energy Display
Voice → Enable**, sign in to Home Assistant, and let Alexa discover devices.

## Manual setup

1. **Lambda** (AWS console, same region as above): Python 3.12, code
   `smart-home/lambda/lambda_function.py`, environment variable `BASE_URL` =
   `https://your-home-assistant`, timeout 8 s.
2. **Skill** (Alexa developer console): Create Skill → *Smart Home*. Copy the skill ID,
   add an **Alexa Smart Home** trigger to the Lambda with that skill ID, then put the
   Lambda ARN into the skill's *Default endpoint* (and your region's endpoint).
3. **Account linking** (Build → Account Linking):

   | Field | Value |
   | --- | --- |
   | Web Authorization URI | `https://your-home-assistant/auth/authorize` |
   | Access Token URI | `https://your-home-assistant/auth/token` |
   | Client ID | `https://layla.amazon.com/` (EU), `https://pitangui.amazon.com/` (Americas), `https://alexa.amazon.co.jp/` (Far East) |
   | Client Secret | anything |
   | Authentication Scheme | Credentials in request body |
   | Scope | `smart_home` |

4. **Permissions** (Build → Permissions): turn on *Send Alexa Events*. Copy the client ID
   and secret into the integration's options as *Smart home skill messaging client ID/secret*.
5. Enable and link the skill in the Alexa app, then discover devices.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Linking fails with "invalid redirect URI" | The client ID doesn't match your Alexa region; see the table above. |
| Linking succeeds, discovery finds nothing | Check the Lambda's CloudWatch log. `HTTP 404` means the integration is older than 0.2.0; `BRIDGE_UNREACHABLE` means `BASE_URL` can't be reached from AWS. |
| Values are "unresponsive" | The source sensors are unavailable; Alexa reports `connectivity: UNREACHABLE` until they return. |
| Routines never trigger | Add the skill's messaging credentials, then disable and re-enable the skill so Alexa sends AcceptGrant again. |
