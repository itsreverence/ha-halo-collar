DOMAIN = "halo_collar"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

# Public OAuth client details extracted from the official Halo mobile app.
# These are static, app-level (not user) credentials shared by every install and
# are required by the Halo IdentityServer to exchange user credentials for tokens.
DEFAULT_CLIENT_ID = "halo.app.android"
DEFAULT_CLIENT_SECRET = "34fcPOX6rChDi83@"
DEFAULT_API_BASE = "https://api.halocollar.com"
DEFAULT_AUTH_BASE = "https://auth.halocollar.com"
DEFAULT_TOKEN_SCOPE = "openid profile offline_access"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL_SECONDS = 300
MIN_SCAN_INTERVAL_SECONDS = 60
MAX_SCAN_INTERVAL_SECONDS = 3600

PLATFORMS = ["sensor", "binary_sensor", "device_tracker"]
