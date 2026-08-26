"""Constants for the YoLink Local integration."""

from datetime import timedelta

DOMAIN = "yolocal"

# Configuration keys
CONF_HUB_IP = "hub_ip"
CONF_SECONDARY_HUB_IP = "secondary_hub_ip"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_NET_ID = "net_id"

# Default ports
DEFAULT_HTTP_PORT = 1080
DEFAULT_MQTT_PORT = 18080

# The integration is MQTT-first.  This timer evaluates cached liveness only;
# it does NOT poll every device.  HTTP getState is queued only for devices that
# are stale/suspect or need command confirmation.
HEALTH_EVALUATION_INTERVAL = timedelta(minutes=1)
DEVICE_DISCOVERY_INTERVAL = timedelta(minutes=1)

# API endpoints
TOKEN_ENDPOINT = "/open/yolink/token"
API_ENDPOINT = "/open/yolink/v2/api"

# Platforms we support
PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "lock",
    "switch",
    "siren",
    "valve",
]
