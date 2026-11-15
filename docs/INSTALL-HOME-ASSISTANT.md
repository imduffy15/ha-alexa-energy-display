# Install the Home Assistant integration

## 1. Install the files

**HACS (recommended)**

1. HACS → ⋮ → **Custom repositories**. Add
   `https://github.com/imduffy15/ha-alexa-energy-display` with type **Integration**.
2. Search for **Alexa Energy Display**, then **Download**.
3. Restart Home Assistant.

**Manually**

Copy `custom_components/alexa_energy_display` into your configuration directory's
`custom_components/` folder, then restart Home Assistant.

## 2. Make sure Alexa can reach Home Assistant

The skill runs in Amazon's cloud and calls one Home Assistant webhook over HTTPS. Use
one of these:

- **Home Assistant Cloud**: when you are signed in with an active subscription, the
  integration creates a cloud webhook automatically. You need no open ports.
- **Your own URL**: set *Settings → System → Network → Home Assistant URL → Internet*,
  for example `https://ha.example.com`, served through your reverse proxy or tunnel.

## 3. Add the integration

*Settings → Devices & services → Add integration → Alexa Energy Display*.

**Step 1: Name and skill credentials**

- **Title**: shown at the top of the display, for example *Energy*.
- **Skill messaging client ID / secret**: leave these empty for now. You get them after
  creating the skill ([INSTALL-SKILL.md](INSTALL-SKILL.md), step 4) and add them later
  under **Configure**. Without them only the full-screen view works.

**Step 2: Data sources**

| Option | Meaning |
| --- | --- |
| Use the Energy dashboard configuration | Reads *Settings → Dashboards → Energy*. Live power tiles come from the power sensors configured there: solar, grid, battery, home. Today's kWh comes from the long-term statistics of the energy meters, since local midnight. Battery state of charge comes from the battery's SoC sensor. |
| Extra entities | Any entities, each shown as a tile with its name, state and unit, in the order you pick them: tariffs, EV battery, inverter mode, hot-water temperature and so on. |
| Show extra entities before energy tiles | Put your own entities first. The **widget always shows the first four tiles**. |
| Dashboard URL | Opened in Silk by "Alexa, ask energy display to open the dashboard", for example `https://ha.example.com/energy`. |
| Update interval | How often the snapshot is rebuilt. The widget is pushed only when a value changed, or every 15 minutes. |
| Only accept this skill ID | Optional hardening. Paste your skill's ID (`amzn1.ask.skill...`, shown in the developer console) to reject calls from anything else. |

After setup, a notification shows the **webhook URL**. You paste it into the skill's
`lambda/config.json`. You can see it again any time under **Configure**.

## Entities

| Entity | Purpose |
| --- | --- |
| `sensor.<title>_widget_status` | `not_paired`, `paired`, `pull_only` (no credentials) or `error`. Attributes: `paired_accounts`, `last_push`, `last_push_result`, `last_error`, `tiles`. |
| `button.<title>_push_to_widget_now` | Rebuild and push immediately. |

## Tips for arbitrary data

- Want a different label, rounding or unit? Create a
  [template sensor](https://www.home-assistant.io/integrations/template/) and pick that.
  The tile uses its friendly name and unit as they are.
- Up to twelve tiles are shown full screen and four in the widget. Further tiles are
  dropped.
- Percentage entities (unit `%`) get a progress bar.
- Several displays: add the integration more than once, each with its own skill and
  webhook.

## Removing

Delete the integration entry. Its webhook, its cloud webhook and its stored Alexa
pairings are removed. In the Alexa developer console you can delete the skill.
