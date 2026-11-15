"""Constants for Alexa Energy Display."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "alexa_energy_display"

CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_CLOUDHOOK_URL: Final = "cloudhook_url"
CONF_TITLE: Final = "title"
CONF_USE_ENERGY: Final = "use_energy_dashboard"
CONF_ENTITIES: Final = "entities"
CONF_ENTITIES_FIRST: Final = "entities_first"
CONF_DASHBOARD_URL: Final = "dashboard_url"
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_SKILL_ID: Final = "skill_id"

DEFAULT_TITLE: Final = "Energy"
DEFAULT_UPDATE_INTERVAL: Final = 60
MIN_UPDATE_INTERVAL: Final = 30
MAX_UPDATE_INTERVAL: Final = 3600

# Data store layout shared with the widget and full-screen APL documents.
DATASTORE_NAMESPACE: Final = "haEnergy"
DATASTORE_KEY: Final = "snapshot"
SNAPSHOT_VERSION: Final = 1
MAX_TILES: Final = 12
WIDGET_TILES: Final = 4

LWA_TOKEN_URL: Final = "https://api.amazon.com/auth/o2/token"
LWA_SCOPE: Final = "alexa::datastore"
DEFAULT_API_ENDPOINT: Final = "https://api.amazonalexa.com"
ALLOWED_API_ENDPOINTS: Final = frozenset(
    {
        "https://api.amazonalexa.com",
        "https://api.eu.amazonalexa.com",
        "https://api.fe.amazonalexa.com",
    }
)

# Re-push an unchanged snapshot this often so the widget's "updated" time stays honest.
MAX_PUSH_AGE_SECONDS: Final = 900
# How long Alexa keeps retrying delivery to an offline device.
DELIVERY_WINDOW_SECONDS: Final = 900

STORAGE_VERSION: Final = 1
