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
import views
from ask_sdk_core.dispatch_components import AbstractExceptionHandler, AbstractRequestHandler
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.skill_builder import SkillBuilder

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

HELP = (
    "Energy Display shows your Home Assistant energy data. Say open energy display, "
    "ask energy display to refresh, or ask energy display to open the dashboard."
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


def show_energy(handler_input: HandlerInput, event_type: str):
    rb = handler_input.response_builder
    try:
        reply = ha_client.call(event_type, identity(handler_input))
    except ha_client.NotConfigured:
        text = "Energy Display is not set up yet. Add your Home Assistant webhook URL to the skill's config file."
        if has_screen(handler_input):
            rb.add_directive(views.render(None, text))
        return rb.speak(text).set_should_end_session(True).response
    except ha_client.HomeAssistantError as err:
        logger.warning("%s", err)
        text = "I couldn't reach Home Assistant."
        if has_screen(handler_input):
            rb.add_directive(views.render(None, text + " Check that it is reachable from the internet."))
        return rb.speak(text).set_should_end_session(True).response

    snapshot = reply.get("snapshot") or {}
    if not has_screen(handler_input):
        return rb.speak(snapshot.get("speech") or "There is no energy data yet.").set_should_end_session(True).response
    rb.add_directive(views.render(snapshot))
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
        for directive in views.open_url(url):
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


sb = SkillBuilder()
for handler in (
    LaunchHandler(),
    RefreshIntentHandler(),
    UserEventHandler(),
    OpenDashboardHandler(),
    PackageManagerHandler(),
    HelpHandler(),
    StopHandler(),
    FallbackHandler(),
    SessionEndedHandler(),
):
    sb.add_request_handler(handler)
sb.add_exception_handler(ErrorHandler())
lambda_handler = sb.lambda_handler()
