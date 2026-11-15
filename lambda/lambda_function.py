"""Alexa Energy Display skill.

"Alexa, open energy display"           -> full-screen tiles, refreshed every minute
"Alexa, ask energy display to refresh" -> same, freshly built
"Alexa, ask energy display to open the dashboard" -> Silk opens the configured URL
Widget tap                             -> full-screen tiles
Every request also pairs the Alexa account with Home Assistant, so Home Assistant
can push widget data to that account.
"""

from __future__ import annotations

import logging
from typing import Any

import ask_sdk_core.utils as ask_utils
import ha_client
import smart_home
import views
from ask_sdk_core.dispatch_components import (
    AbstractExceptionHandler,
    AbstractRequestHandler,
    AbstractRequestInterceptor,
)
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.skill_builder import SkillBuilder

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

HELP = (
    "Energy Display shows your Home Assistant energy data. Open it to see the display, "
    "or ask about the battery, solar, the grid, your home usage, or today's totals."
)


def identity(handler_input: HandlerInput) -> dict[str, Any]:
    system = handler_input.request_envelope.context.system
    return {
        "user_id": system.user.user_id if system.user else None,
        "device_id": system.device.device_id if system.device else None,
        "api_endpoint": system.api_endpoint,
        "skill_id": system.application.application_id if system.application else None,
    }


def has_screen(handler_input: HandlerInput) -> bool:
    device = handler_input.request_envelope.context.system.device
    interfaces = device.supported_interfaces if device else None
    return getattr(interfaces, "alexa_presentation_apl", None) is not None


def _enum_value(value) -> str | None:
    return getattr(value, "value", value)


def screen_of(handler_input: HandlerInput) -> views.Screen:
    """The requesting device's display, from context.Viewport."""
    vp = handler_input.request_envelope.context.viewport
    if vp is None:
        return views.Screen()
    return views.Screen(
        width=vp.current_pixel_width or vp.pixel_width or 1280,
        height=vp.current_pixel_height or vp.pixel_height or 800,
        shape=_enum_value(vp.shape) or "RECTANGLE",
        mode=_enum_value(vp.mode) or "HUB",
    )


def apl_version(handler_input: HandlerInput) -> str | None:
    device = handler_input.request_envelope.context.system.device
    apl = getattr(device.supported_interfaces, "alexa_presentation_apl", None) if device else None
    runtime = getattr(apl, "runtime", None)
    return getattr(runtime, "max_version", None)


def render(handler_input: HandlerInput, snapshot, message=None):
    return views.render(snapshot, message, screen_of(handler_input), apl_version(handler_input))


def show_energy(handler_input: HandlerInput, event_type: str):
    rb = handler_input.response_builder
    try:
        reply = ha_client.call(event_type, identity(handler_input))
    except ha_client.NotConfigured as err:
        logger.warning("Not configured: %s", err)
        text = "Energy Display is not set up yet. Add your Home Assistant webhook URL to the skill's config file."
        if has_screen(handler_input):
            rb.add_directive(render(handler_input, None, text))
        return rb.speak(text).set_should_end_session(True).response
    except ha_client.HomeAssistantError as err:
        logger.warning("%s", err)
        text = "I couldn't reach Home Assistant."
        if has_screen(handler_input):
            rb.add_directive(render(handler_input, None, text + " Check that it is reachable from the internet."))
        return rb.speak(text).set_should_end_session(True).response

    snapshot = reply.get("snapshot") or {}
    if not has_screen(handler_input):
        return rb.speak(snapshot.get("speech") or "There is no energy data yet.").set_should_end_session(True).response
    rb.add_directive(render(handler_input, snapshot))
    # Leave shouldEndSession unset: the document stays on screen without opening the
    # microphone, and its tick handler sends "refresh" events.
    return rb.response


class LaunchHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        return show_energy(handler_input, "launch")


class RefreshIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("RefreshIntent")(handler_input)

    def handle(self, handler_input):
        return show_energy(handler_input, "refresh")


class UserEventHandler(AbstractRequestHandler):
    """APL SendEvent: 'refresh' from the full-screen tick, 'open' from a widget tap."""

    def can_handle(self, handler_input):
        return ask_utils.is_request_type("Alexa.Presentation.APL.UserEvent")(handler_input)

    def handle(self, handler_input):
        args = getattr(handler_input.request_envelope.request, "arguments", None) or []
        return show_energy(handler_input, "refresh" if args[:1] == ["refresh"] else "launch")


# Which tiles answer a question about each metric (tile ids from the integration).
METRIC_TILES = {
    "battery": ("battery", "battery_today"),
    "solar": ("solar_now", "solar_today"),
    "grid": ("grid_now", "import_today", "export_today"),
    "home": ("home_now", "home_today"),
    "today": ("solar_today", "import_today", "export_today", "home_today", "battery_today"),
}
SPOKEN_UNITS = {"kW": "kilowatts", "W": "watts", "kWh": "kilowatt hours", "MWh": "megawatt hours", "%": "percent"}


def _metric(handler_input: HandlerInput) -> tuple[str | None, str | None]:
    """(resolved metric id, what the user said)."""
    slot = ask_utils.request_util.get_slot(handler_input, "metric")
    if slot is None or not slot.value:
        return None, None
    try:
        for res in slot.resolutions.resolutions_per_authority:
            if res.status.code.value == "ER_SUCCESS_MATCH":
                return res.values[0].value.id, slot.value
    except (AttributeError, IndexError, TypeError):
        pass
    return None, slot.value


def describe(tiles: list[dict], metric: str | None, said: str | None) -> str | None:
    """Speech for the tiles that answer the question, or None if nothing matches."""
    wanted = METRIC_TILES.get(metric or "")
    chosen = [t for t in tiles if t.get("id") in wanted] if wanted else []
    if not chosen and said:  # extra entities: match on their label
        chosen = [t for t in tiles if said.lower() in t.get("label", "").lower()]
    if not chosen:
        return None
    parts = []
    for t in chosen:
        unit = SPOKEN_UNITS.get(t.get("unit", ""), t.get("unit", ""))
        value = t.get("value", "").replace("/", " in, ")
        when = t.get("sub") or ""
        for short, spoken in (("kW", "kilowatts"), ("W", "watts")):
            if when.endswith(f" {short}"):
                when = when[: -len(short)] + spoken
        if t.get("id") == "battery" and when:
            parts.append(f"{t['label']} is at {value} {unit}, {when}")
        elif when in ("now", "today"):
            parts.append(f"{t['label']} {when} is {value} {unit}")
        else:
            parts.append(f"{t['label']} is {value} {unit}")
    return ". ".join(p.replace("  ", " ").strip() for p in parts) + "."


class EnergyQuestionHandler(AbstractRequestHandler):
    """'Alexa, ask energy display about the battery' and similar."""

    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("EnergyQuestionIntent")(handler_input)

    def handle(self, handler_input):
        rb = handler_input.response_builder
        try:
            reply = ha_client.call("refresh", identity(handler_input))
        except ha_client.NotConfigured:
            return rb.speak("Energy Display is not set up yet.").set_should_end_session(True).response
        except ha_client.HomeAssistantError:
            return rb.speak("I couldn't reach Home Assistant.").set_should_end_session(True).response
        snapshot = reply.get("snapshot") or {}
        metric, said = _metric(handler_input)
        text = describe(snapshot.get("tiles") or [], metric, said)
        if text is None:
            text = f"I don't have {said} on this display." if said else (snapshot.get("speech") or "No data yet.")
        return rb.speak(text).set_should_end_session(True).response


class OpenDashboardHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("OpenDashboardIntent")(handler_input)

    def handle(self, handler_input):
        rb = handler_input.response_builder
        if not has_screen(handler_input):
            return rb.speak("This device has no screen.").set_should_end_session(True).response
        try:
            reply = ha_client.call("open_dashboard", identity(handler_input))
        except ha_client.HomeAssistantError:
            return rb.speak("I couldn't reach Home Assistant.").set_should_end_session(True).response
        url = reply.get("dashboard_url")
        if not url:
            return (
                rb.speak("No dashboard URL is set in the Home Assistant integration.")
                .set_should_end_session(True)
                .response
            )
        for directive in views.open_url(url, apl_version(handler_input)):
            rb.add_directive(directive)
        return rb.response


class PackageManagerHandler(AbstractRequestHandler):
    """Widget install/remove/error notices (sent for live skills only)."""

    TYPES = {
        "Alexa.DataStore.PackageManager.UsagesInstalled": "widget_installed",
        "Alexa.DataStore.PackageManager.UsagesRemoved": "widget_removed",
        "Alexa.DataStore.PackageManager.InstallationError": "datastore_error",
        "Alexa.DataStore.Error": "datastore_error",
        "Alexa.DataStore.DataStoreError": "datastore_error",
        "Alexa.DataStore.PackageManager.UpdateRequest": "widget_installed",
    }

    def can_handle(self, handler_input):
        return handler_input.request_envelope.request.object_type in self.TYPES

    def handle(self, handler_input):
        request = handler_input.request_envelope.request
        event_type = self.TYPES[request.object_type]
        detail = None
        if event_type == "datastore_error":
            error = getattr(request, "error", None)
            detail = {"type": getattr(error, "object_type", None) or str(error)[:200]}
        try:
            ha_client.call(event_type, identity(handler_input), detail)
        except ha_client.HomeAssistantError as err:
            logger.warning("%s", err)
        return handler_input.response_builder.response


class HelpHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.speak(HELP).ask("What would you like?").response


class StopHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return any(
            ask_utils.is_intent_name(n)(handler_input)
            for n in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent")
        )

    def handle(self, handler_input):
        return handler_input.response_builder.set_should_end_session(True).response


class FallbackHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.speak(HELP).set_should_end_session(True).response


class SessionEndedHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.response


class ErrorHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(exception, exc_info=True)
        return (
            handler_input.response_builder.speak("Sorry, Energy Display had a problem.")
            .set_should_end_session(True)
            .response
        )


class LogRequestInterceptor(AbstractRequestInterceptor):
    """One line per request in CloudWatch, without IDs or tokens."""

    def process(self, handler_input):
        envelope = handler_input.request_envelope
        request = envelope.request
        intent = getattr(getattr(request, "intent", None), "name", None)
        screen = screen_of(handler_input)
        logger.info(
            "Request %s%s locale=%s apl=%s viewport=%sx%s %s %s",
            request.object_type,
            f" intent={intent}" if intent else "",
            getattr(request, "locale", None),
            apl_version(handler_input) if has_screen(handler_input) else "none",
            screen.width,
            screen.height,
            screen.shape,
            screen.mode,
        )


sb = SkillBuilder()
sb.add_global_request_interceptor(LogRequestInterceptor())
for handler in (
    LaunchHandler(),
    RefreshIntentHandler(),
    UserEventHandler(),
    OpenDashboardHandler(),
    EnergyQuestionHandler(),
    PackageManagerHandler(),
    HelpHandler(),
    StopHandler(),
    FallbackHandler(),
    SessionEndedHandler(),
):
    sb.add_request_handler(handler)
sb.add_exception_handler(ErrorHandler())
_ask_handler = sb.lambda_handler()


def lambda_handler(event, context):
    """Custom-skill requests go to the ASK SDK; smart home directives (when this skill
    is a multi-capability skill with the smart home API) go to Home Assistant."""
    if isinstance(event, dict) and "directive" in event:
        return smart_home.forward(event)
    return _ask_handler(event, context)
