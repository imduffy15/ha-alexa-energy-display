# How it works

## Pieces

| Piece | Runs in | Does |
| --- | --- | --- |
| Skill (`lambda/`) | Your Alexa-hosted Lambda | Handles voice, renders the full-screen APL document, forwards widget lifecycle events. Stateless; its only setting is the webhook URL. |
| Widget (`skill-package/dataStorePackages/EnergyWidget`) | The Echo | An APL document bound to the on-device data store key `haEnergy/snapshot`. It never calls anything; it redraws when the key changes. |
| Integration (`custom_components/alexa_energy_display`) | Home Assistant | Builds the snapshot, answers the webhook, remembers paired Alexa accounts, pushes the snapshot through the Data Store REST API. |

## Pairing

Amazon only tells a skill who its users are when they talk to it. It sends a widget
install notice (`UsagesInstalled`) only for published skills. So every skill request
POSTs this to the webhook:

```json
{"type": "launch", "user_id": "amzn1.ask.account…", "device_id": "amzn1.ask.device…",
 "api_endpoint": "https://api.eu.amazonalexa.com", "skill_id": "amzn1.ask.skill…"}
```

The integration stores `user_id` and the regional endpoint in `.storage/`. It accepts
only the three Amazon endpoints; anything else falls back to North America. Pushes
target `{"type": "USER", "id": user_id}`, which reaches every widget-capable Echo on
that account. The IDs are specific to your skill and are useless to other skills.

Event types: `launch`, `refresh` (the full-screen view's one-minute timer, or "refresh"
by voice), `open_dashboard`, `widget_installed`, `widget_removed`, `datastore_error`.
Every response is:

```json
{"snapshot": { … }, "dashboard_url": "https://…" , "widget": true}
```

## Snapshot schema (v1)

Values are formatted in Home Assistant, so the APL documents only lay out strings.

```json
{
  "v": 1,
  "title": "Energy",
  "updated": "14:05",
  "updated_at": "2026-11-15T14:05:00+00:00",
  "status": "ok",
  "tiles": [
    {"id": "solar_now", "label": "Solar", "value": "3.42", "unit": "kW", "color": "#F5B700", "sub": "now"},
    {"id": "battery", "label": "Battery", "value": "76", "unit": "%", "color": "#4DA3FF",
     "sub": "charging 0.95 kW", "percent": 76}
  ],
  "widget": ["first four tiles"],
  "speech": "Solar now: 3.42 kilowatts. Battery: 76 percent."
}
```

`tests/test_contract.py` fails if an APL document binds to a field the integration
doesn't produce.

### Energy dashboard mapping

| Tile | Source in the Energy preferences |
| --- | --- |
| Solar now | Sum of solar `stat_rate` power sensors |
| Grid import / export now | Grid `stat_rate` (positive = import), unified and legacy formats |
| Battery | `stat_soc` average, with charge/discharge from battery `stat_rate` (positive = discharge) |
| Home now | Home source `stat_rate`, otherwise solar + grid + battery |
| Solar / Imported / Exported today | `change` of each `stat_energy_*` statistic since local midnight, in kWh |
| Home used today | Home source, otherwise solar + import − export + discharge − charge |
| Battery in / out today | Battery `stat_energy_to` / `stat_energy_from` |

Missing or unavailable sources are skipped, never shown as zero.

## Push policy

- Rebuild every *update interval* (default 60 s).
- Push when any tile changed, or when the last push is 15 minutes old, so the
  widget's time stays honest. Push immediately when a new account pairs, and when
  **Push to widget now** is pressed.
- `attemptDeliveryUntil` is 15 minutes. Alexa queues the update for an Echo that is
  offline, and drops it once it would be stale anyway.
- Amazon's limits: 16 KB per call (a full snapshot is about 3 KB), 1 MB per skill per
  device, 25 writes per second.

## Security

- The webhook ID (256 bits) is the only secret the skill holds. It allows reading the
  snapshot and adding Alexa accounts as push targets; it allows no control of the home.
  If it leaks, delete and re-add the integration to get a new one.
- Optionally restrict to your skill ID (`skill_id` option). The skill ID is not secret,
  so this is defence in depth, not authentication.
- The skill messaging client secret stays in Home Assistant. Diagnostics redact it,
  along with the webhook and client ID.
- No Home Assistant access token is created or shared.

## Full-screen refresh

`lambda/apl/fullscreen.json` has a `handleTick` handler. Every 60 s it sends a
`refresh` event, and the skill answers with a newly rendered document. The response
leaves `shouldEndSession` unset, so the microphone stays closed while the display stays
up. `settings.idleTimeout` asks the Echo to keep the session for 10 minutes between
events. Amazon may still end long sessions.

## Ideas not built

- **Camera stream**: render a dashboard to video and expose it as an Alexa camera.
  Heavy (encoding, a camera bridge), times out like Silk, and gains nothing over the
  native view.
- **Amazon Photos**: upload dashboard screenshots to the photo frame album. There is no
  official upload API, and Amazon picks up new photos with a delay.
