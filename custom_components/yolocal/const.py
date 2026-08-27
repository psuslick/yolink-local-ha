"""Constants for the YoLink Local integration."""

from datetime import timedelta

DOMAIN = "yolocal"
YOLINK_EVENT = "yolocal_event"

# Configuration keys
CONF_HUB_IP = "hub_ip"
CONF_SECONDARY_HUB_IP = "secondary_hub_ip"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_NET_ID = "net_id"

# Default ports
DEFAULT_HTTP_PORT = 1080
DEFAULT_MQTT_PORT = 18080

# The integration is MQTT-first. This timer evaluates cached liveness only;
# it does NOT poll every device. HTTP getState is queued only for devices that
# are stale/suspect or need command confirmation.
HEALTH_EVALUATION_INTERVAL = timedelta(minutes=1)
DEVICE_DISCOVERY_INTERVAL = timedelta(minutes=1)

# API endpoints
TOKEN_ENDPOINT = "/open/yolink/token"
API_ENDPOINT = "/open/yolink/v2/api"

# Validated physical-button models / trigger types.
YS5708_MODELS = ("YS5708-UC", "YS5708-EC")
BUTTON_1_SHORT_PRESS = "button_1_short_press"
BUTTON_1_LONG_PRESS = "button_1_long_press"
BUTTON_2_SHORT_PRESS = "button_2_short_press"
BUTTON_2_LONG_PRESS = "button_2_long_press"
YS5708_TRIGGER_TYPES = (
    BUTTON_1_SHORT_PRESS,
    BUTTON_1_LONG_PRESS,
    BUTTON_2_SHORT_PRESS,
    BUTTON_2_LONG_PRESS,
)

# Platforms we support
PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "light",
    "lock",
    "switch",
    "siren",
    "valve",
]
