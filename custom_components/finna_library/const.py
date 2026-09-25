"""Constants for the Finna Library integration."""

DOMAIN = "finna_library"

DEFAULT_HOST = "heili.finna.fi"
USER_AGENT = "Mozilla/5.0 (compatible; HomeAssistant finna_library)"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PIN = "pin"

UPDATE_INTERVAL_HOURS = 6
# After a failed poll, retry after these delays before falling back to the
# normal interval: an overnight Finna slowdown shouldn't cost 6 h (issue #4).
RETRY_DELAYS_MINUTES = (5, 15, 30)
