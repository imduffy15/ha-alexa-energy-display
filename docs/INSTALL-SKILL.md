# Install the Alexa skill and widget

This creates a private copy of the skill in your own Amazon developer account. You
never submit it for certification. It stays in the **development** stage, where Amazon
lets you use it on every Echo signed in to the same Amazon account.

Install the Home Assistant integration first
([INSTALL-HOME-ASSISTANT.md](INSTALL-HOME-ASSISTANT.md)). You need its webhook URL in
step 3.

## 1. Create the skill

1. Open the [Alexa developer console](https://developer.amazon.com/alexa/console/ask).
   Sign in with **the same Amazon account your Echo uses**, creating a developer account
   if asked (free).
2. **Create Skill**.
   - **Name**: `Energy Display`.
   - **Primary locale**: the language your Echo uses. In the UK and Ireland that is
     *English (UK)*. If they differ, Alexa answers "I can't find that skill".
   - **Experience**: *Other*. **Model**: *Custom*.
   - **Hosting service**: *Alexa-hosted (Python)*. Choose the hosting region nearest
     you, for example *EU (Ireland)*.
3. **Templates**: choose **Import skill** and enter

   ```
   https://github.com/imduffy15/ha-alexa-energy-display.git
   ```

   then **Import**. The console copies `skill-package/` (manifest, interaction models,
   widget) and `lambda/` (code) into your skill. This takes a minute or two.

> If the import fails, choose **Start from scratch** instead and follow
> [Manual setup](#manual-setup-if-import-fails) below, then continue with step 2.

## 2. Check the build

On the **Build** tab:

- **Invocations → Skill Invocation Name** is `energy display`. You can change it, for
  example to `my energy`. Then you say "Alexa, open my energy".
- **Interfaces**: *Alexa Presentation Language*, *Data Store* and *Data Store Packages*
  are on.
- Press **Build skill** and wait for "Build successful".

## 3. Point the skill at Home Assistant

1. Open the **Code** tab.
2. Next to `lambda/`, create a file named `config.json`:

   ```json
   {
     "webhook_url": "https://YOUR-HOME-ASSISTANT/api/webhook/...",
     "timeout_seconds": 6
   }
   ```

   Paste the webhook URL that Home Assistant showed in its notification. You can also
   find it under **Settings → Devices & services → Alexa Energy Display → Configure**.
   Keep this URL private: anyone who has it can read your energy tiles.
3. **Save**, then **Deploy**.

## 4. Enable the widget push

1. In the Build tab's left menu, open **Tools → Permissions**.
2. Under **Alexa Skill Messaging**, copy the **Client Id** and **Client Secret**.
3. In Home Assistant, open **Settings → Devices & services → Alexa Energy Display →
   Configure**. Paste them into *Skill messaging client ID* and *secret*, then submit.
   Home Assistant checks them with Login with Amazon straight away.

Without these, the full-screen view still works, but the widget never receives data.

## 5. Turn on testing and pair your Echo

1. On the **Test** tab, set *Skill testing is enabled in* to **Development**.
2. On your Echo Show, say **"Alexa, open energy display"**. You should see the tiles
   full screen.

   This first call also pairs your Alexa account with Home Assistant. The *Widget
   status* sensor changes to **Paired**, and the first widget push happens immediately.
3. Say "Alexa, stop" or swipe away to leave.

## 6. Put the widget on the Echo

Amazon installs development-stage widgets from the console's widget authoring tool:

1. In the developer console, open your skill and go to **Build → Multimodal → Widgets**.
   If the import created it, *Home Energy* (`EnergyWidget`) is listed.
2. Open it. The authoring tool shows a preview with no data, because Preview does not
   use the data store.
3. At the bottom, choose **Install** (some console versions say **Send to Device**) and
   pick your Echo Show.
4. On the Echo:
   - **Echo Show 15/21**: the widget appears in the Widget Panel.
   - **Echo Show 8/10**: tap the widget shortcut on the home screen, or swipe to the
     widgets page.

   Tap the widget to open the full-screen view.

If the widget shows *"Say 'Alexa, open energy display' to connect"*, it has not
received data yet. Open the skill once, or press **Push to widget now** in Home
Assistant, then wait a minute.

## Manual setup (if import fails)

Start from a blank Alexa-hosted (Python) skill, then:

1. **Interaction model**: *Build → Interaction Model → JSON Editor*. Paste
   `skill-package/interactionModels/custom/<your-locale>.json` and **Save**.
2. **Interfaces**: *Build → Interfaces*. Turn on *Alexa Presentation Language* (tick
   all Hub devices), *Data Store* and *Data Store Packages*. **Save**.
3. **Extension**: the full manifest is in `skill-package/skill.json`. If the console
   offers *Build → Manifest* (JSON editor), paste the `apis` block from that file,
   which adds the `alexaext:datastore:10` extension. Otherwise use the ASK CLI below.
4. **Code**: on the Code tab, replace `lambda/lambda_function.py` and
   `lambda/requirements.txt`, and add `lambda/ha_client.py`, `lambda/views.py`,
   `lambda/apl/fullscreen.json` and `lambda/apl/empty.json` from this repo. Add your
   `lambda/config.json`, then **Deploy**.
5. **Widget**: *Build → Multimodal → Widgets → Create widget → Blank*. Set the ID to
   `EnergyWidget`. Paste
   `skill-package/dataStorePackages/EnergyWidget/documents/document.json` into the APL
   editor, and `datasources/default.json` into Data. Fill in the name and description,
   then save and associate the widget with this skill.
6. **Build skill**, then carry on with step 3 above.

## Using the ASK CLI instead

If you prefer the command line and have an AWS account, `ask-resources.json` deploys
the skill and an AWS Lambda in your own account:

```sh
npm install -g ask-cli && ask configure
git clone https://github.com/imduffy15/ha-alexa-energy-display && cd ha-alexa-energy-display
ask deploy
```

Then set the Lambda environment variable `WEBHOOK_URL` in the AWS console, instead of
using `config.json`. The variable takes precedence over the file.

## Updating

Alexa-hosted skills keep their code in a private repository that Amazon creates.
To update, copy the changed files from this repo into the Code tab and **Deploy**. Or
clone that repository with `ask init --hosted-skill-id <id>`, then merge from this
repo. Changes under `skill-package/` need **Build skill** afterwards.
Bump `manifest.version` in the widget's `manifest.json` when you change the widget.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "I can't find that skill" | Skill locale differs from the Echo's language, or Test is not set to Development. |
| "Energy Display is not set up yet" | `lambda/config.json` is missing or still has the placeholder URL; Deploy after saving. |
| "I couldn't reach Home Assistant" | The webhook URL must work from the internet. Try `curl -X POST -d '{}' <url>`: it should return JSON with a `snapshot`. |
| Widget status **Error** with `DATA_STORE_SUPPORT_REQUIRED` | Turn on *Data Store* in Build → Interfaces and rebuild. |
| Widget status **Error** with credentials rejected | Copy the Alexa Skill Messaging client ID/secret again (Tools → Permissions). |
| Widget never updates | Check *Widget status* attributes: `paired_accounts` must be ≥ 1 and `last_push_result` should say `delivered`. A paired account with "accepted, no devices with the widget yet" means the widget is not installed on any Echo. |
| Full screen returns to the home screen | Amazon ended the session. Open it again, or use the widget. |
