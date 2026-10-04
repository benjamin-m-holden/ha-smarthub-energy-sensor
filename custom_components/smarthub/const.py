"""Constants for the SmartHub integration."""

DOMAIN = "smarthub"

# Configuration keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCOUNT_ID = "account_id"
CONF_HOST = "host"
CONF_POLL_INTERVAL = "poll_interval"
CONF_TIMEZONE = "timezone"
CONF_MFA_TOTP = "mfa_totp"
CONF_ELECTRIC_RATE = "electric_rate"
CONF_WATER_RATE = "water_rate"
CONF_ELECTRIC_BASE_CHARGE = "electric_base_charge"
CONF_WATER_BASE_CHARGE = "water_base_charge"

# Default values
DEFAULT_POLL_INTERVAL = 360  # 6 hour in minutes
MIN_POLL_INTERVAL = 15  # Minimum 15 minutes
MAX_POLL_INTERVAL = 1440  # Maximum 24 hours

# API constants
DEFAULT_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
SESSION_TIMEOUT = 300  # 5 minutes - force session refresh
HISTORICAL_IMPORT_DAYS = 90 # number of days for initial import

# Sensor constants
USAGE_SENSOR_KEY = "current_energy_usage"
ATTR_LAST_READING_TIME = "last_reading_time"
ATTR_ACCOUNT_ID = "account_id"
ATTR_LOCATION_ID = "location_id"
ATTR_INDUSTRY = "industry"
LOCATION_KEY = "location"
METER_NAME   = "meter_name"

# Industries supported by the smarthub utility-usage endpoint. Keys are the
# exact uppercase strings SmartHub uses in the poll request/response.
INDUSTRY_ELECTRIC = "ELECTRIC"
INDUSTRY_WATER = "WATER"
SUPPORTED_INDUSTRIES = [INDUSTRY_ELECTRIC, INDUSTRY_WATER]

# Per-industry service discovery: how to recognize each industry's service
# key in a SmartHub account's serviceToServiceDescription/services fields.
INDUSTRY_DISCOVERY = {
    INDUSTRY_ELECTRIC: {
        "service_description_match": "electric",
        "fallback_services": ["ELEC", "1ELEC", "VELEC", "GELEC"],
    },
    INDUSTRY_WATER: {
        "service_description_match": "water",
        "fallback_services": ["WATER", "WATR", "WTR", "1WATER", "1WATR", "VWATR", "GWATR"],
    },
}

INDUSTRY_STAT_PREFIX = {
    INDUSTRY_ELECTRIC: "smarthub_energy",
    INDUSTRY_WATER: "smarthub_water",
}

# Optional per-industry unit rates (price per kWh / per gallon). SmartHub's
# utility-usage endpoint only returns usage, never billed cost, so cost
# statistics are derived as consumption * rate when a rate is configured.
# Without a rate, no cost statistic is produced for that industry.
INDUSTRY_RATE_KEY = {
    INDUSTRY_ELECTRIC: CONF_ELECTRIC_RATE,
    INDUSTRY_WATER: CONF_WATER_RATE,
}

# Optional fixed monthly charges (service availability / base / meter charges).
# These are billed per month regardless of usage, so they cannot be expressed as
# a unit rate. When set, the charge is spread evenly across the statistic
# periods of each month so a month's cost statistics sum to the real fixed
# charge plus volumetric cost.
INDUSTRY_BASE_CHARGE_KEY = {
    INDUSTRY_ELECTRIC: CONF_ELECTRIC_BASE_CHARGE,
    INDUSTRY_WATER: CONF_WATER_BASE_CHARGE,
}
INDUSTRY_LABEL = {
    INDUSTRY_ELECTRIC: "Energy",
    INDUSTRY_WATER: "Water",
}
