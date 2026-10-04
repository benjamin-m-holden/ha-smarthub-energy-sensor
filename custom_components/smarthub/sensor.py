"""SmartHub energy sensor platform."""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:
    from enum import Enum
    class StatisticMeanType(str, Enum):
        NONE = "none"
        MEAN = "mean"
        MAX = "max"
        MIN = "min"


from .api import Aggregation, SmartHubAPI, SmartHubLocation
from .exceptions import (
    SmartHubAuthenticationError,
    SmartHubError as SmartHubAPIError,
)
from .const import (
    DOMAIN,
    USAGE_SENSOR_KEY,
    ATTR_LAST_READING_TIME,
    ATTR_ACCOUNT_ID,
    ATTR_LOCATION_ID,
    ATTR_INDUSTRY,
    LOCATION_KEY,
    HISTORICAL_IMPORT_DAYS,
    METER_NAME,
    INDUSTRY_ELECTRIC,
    INDUSTRY_WATER,
    INDUSTRY_STAT_PREFIX,
    INDUSTRY_LABEL,
    INDUSTRY_RATE_KEY,
    INDUSTRY_BASE_CHARGE_KEY,
)

_LOGGER = logging.getLogger(__name__)

# Per-industry sensor presentation: device class, unit of measurement, the
# recorder's unit-conversion class (required for external statistics), and icon.
INDUSTRY_SENSOR_SPEC = {
    INDUSTRY_ELECTRIC: {
        "device_class": SensorDeviceClass.ENERGY,
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "unit_class": EnergyConverter.UNIT_CLASS,
        "icon": "mdi:lightning-bolt",
    },
    INDUSTRY_WATER: {
        "device_class": SensorDeviceClass.WATER,
        "unit": UnitOfVolume.GALLONS,
        "unit_class": VolumeConverter.UNIT_CLASS,
        "icon": "mdi:water",
    },
}


def _location_data_key(location: SmartHubLocation) -> str:
    """Key used to store/look up a location's data in coordinator.data.

    A single serviceLocationNumber can carry more than one industry (e.g. an
    account with both ELECTRIC and WATER at the same location id), so the
    industry must be part of the key or the two would collide.
    """
    return f"{location.id}_{location.industry}"


def _period_share_of_month(reading_time: datetime, aggregation: Aggregation) -> float:
    """Fraction of a fixed monthly charge attributable to one statistic period.

    Fixed charges (service availability / base / meter charges) are billed per
    month regardless of usage, so they are spread evenly across that month's
    periods - one month's periods therefore sum back to the full charge.
    """
    days_in_month = calendar.monthrange(reading_time.year, reading_time.month)[1]

    if aggregation is Aggregation.HOURLY:
        return 1.0 / (days_in_month * 24)
    if aggregation is Aggregation.DAILY:
        return 1.0 / days_in_month
    # MONTHLY - the whole charge lands on the single period.
    return 1.0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SmartHub sensor platform."""
    _LOGGER.debug("Setting up SmartHub sensor platform")

    config: Dict[str, Any] = config_entry.data

    coordinator = config_entry.runtime_data

    # Ensure that it is the smartHub coordinator
    assert type(coordinator) is SmartHubDataUpdateCoordinator

    last_locations_consumption = coordinator.data.values()

    # Create sensor entities for each location
    entities = []
    for last_consumption in last_locations_consumption:
      entities.append(
          SmartHubUsageSensor(
              coordinator=coordinator,
              config_entry=config_entry,
              config=config,
              location=last_consumption.get(LOCATION_KEY),
          )
      )

    async_add_entities(entities)
    _LOGGER.debug(f"{len(entities)} SmartHub sensor entities added successfully")


class SmartHubDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching SmartHub data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SmartHubAPI,
        update_interval: timedelta,
        config_entry: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=update_interval,
        )
        self.api = api
        self.account_id = config_entry.data.get('account_id','unknown')
        self._config_data = config_entry.data

    def _positive_float_option(self, key: Optional[str]) -> Optional[float]:
        """Return a positive float config option, or None if unset/invalid."""
        if key is None:
            return None

        value = self._config_data.get(key)
        if value is None or value == "":
            return None

        try:
            value = float(value)
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring invalid %s value %r", key, value)
            return None

        return value if value > 0 else None

    def _rate_for_industry(self, industry: str) -> Optional[float]:
        """Return the configured unit rate for an industry, or None if unset.

        SmartHub only returns usage, never cost, so cost statistics are derived
        from this rate.
        """
        return self._positive_float_option(INDUSTRY_RATE_KEY.get(industry))

    def _base_charge_for_industry(self, industry: str) -> Optional[float]:
        """Return the configured fixed monthly charge, or None if unset."""
        return self._positive_float_option(INDUSTRY_BASE_CHARGE_KEY.get(industry))

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data from the SmartHub API."""
        try:
            _LOGGER.debug("Fetching data from SmartHub API")

            # force a logout of the session
            self.api.token = None

            locations = await self.api.get_service_locations()
            if not locations:
                _LOGGER.warning(
                    "No locations found for account %s - no sensors will be created. "
                    "Check that the account_id and host are correct.",
                    self.account_id,
                )

            entity_response = {}

            for location in locations:
              # Because SmartHub provides historical usage/cost with delay of a
              # number of hours we need to insert data into statistics.
              await self._insert_statistics(location, Aggregation.HOURLY)
              await self._insert_statistics(location, Aggregation.DAILY)

              # Fetch monthly information for entity value
              first_day_of_current_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

              data = await self.api.get_energy_data(location=location, start_datetime=first_day_of_current_month, aggregation=Aggregation.MONTHLY)

              data_key = _location_data_key(location)

              if data.get("USAGE", None) is None or len(data.get("USAGE", None)) == 0:
                  _LOGGER.warning("No data received from SmartHub API for location %s", location)
                  # Return previous data if available, otherwise empty dict
                  entity_response[data_key] = {
                    USAGE_SENSOR_KEY: 0, # no data - no usage for the entity.
                    ATTR_LAST_READING_TIME: first_day_of_current_month.replace(tzinfo=ZoneInfo(self.api.timezone)), # use the TZ from the entity so it has consistent formating like 2026-02-01T00:00:00-05:00
                    LOCATION_KEY: location,
                    METER_NAME: data.get(METER_NAME, None)
                  }
                  continue

              last_reading = data.get("USAGE")[-1]
              _LOGGER.debug("Successfully fetched data: %s for location: %s", last_reading, location)

              entity_response[data_key] = {
                USAGE_SENSOR_KEY: last_reading['consumption'],
                ATTR_LAST_READING_TIME: last_reading['reading_time'],
                LOCATION_KEY: location,
                METER_NAME: data.get(METER_NAME, None)
              }

            return entity_response

        except SmartHubAuthenticationError as e:
            _LOGGER.error("Authentication error fetching SmartHub data: %s", e)
            # For auth errors, we want to raise UpdateFailed to trigger retry
            # but also ensure the API will refresh authentication on next attempt
            raise UpdateFailed(f"Authentication failed: {e}") from e
        except SmartHubAPIError as e:
            _LOGGER.error("Error fetching data from SmartHub API: %s", e)
            raise UpdateFailed(f"Error communicating with SmartHub API: {e}") from e
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching SmartHub data: %s", e)
            raise UpdateFailed(f"Unexpected error: {e}") from e


    # https://github.com/tronikos/opower/ was used as a model for how to populate
    # hourly metrics when access to realtime information is not possible via
    # utility dashboards.
    async def _insert_statistics(self, location, aggregation: Aggregation):
        """Retrieve energy usage data asynchronously with retry logic. Always backfills the data overwriting the history based on the collection window."""
        stat_prefix = INDUSTRY_STAT_PREFIX[location.industry]
        industry_label = INDUSTRY_LABEL[location.industry]
        spec = INDUSTRY_SENSOR_SPEC[location.industry]

        # For ELECTRIC this reproduces the pre-existing "smarthub_energy_sensor..." id
        # format byte-for-byte so existing statistic history keeps resolving to it.
        consumption_statistic_id = f"{DOMAIN}:{stat_prefix}_sensor{aggregation.suffix}_{self.account_id}_{location.id}"
        return_statistic_id = f"{DOMAIN}:{stat_prefix}_return_sensor{aggregation.suffix}_{self.account_id}_{location.id}"
        cost_statistic_id = f"{DOMAIN}:{stat_prefix}_cost_sensor{aggregation.suffix}_{self.account_id}_{location.id}"

        # Cost is only produced when the user configured a unit rate and/or a
        # fixed monthly charge for this industry.
        rate = self._rate_for_industry(location.industry)
        base_charge = self._base_charge_for_industry(location.industry)
        track_cost = rate is not None or base_charge is not None

        consumption_unit_class = spec["unit_class"]
        consumption_unit = spec["unit"]
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{location.provider} SmartHub {industry_label} {aggregation.label} Usage - {self.account_id} - {location.description}",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=consumption_unit_class, # required in 2025.11
            unit_of_measurement=consumption_unit,
        )

        return_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{location.provider} SmartHub {industry_label} {aggregation.label} Return - {self.account_id} - {location.description}",
            source=DOMAIN,
            statistic_id=return_statistic_id,
            unit_class=consumption_unit_class, # required in 2025.11
            unit_of_measurement=consumption_unit,
        )

        # Monetary statistics carry no unit/unit_class - this matches how the
        # core opower integration declares its cost statistics, and is what lets
        # the Energy dashboard accept them in a source's "cost" slot.
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{location.provider} SmartHub {industry_label} {aggregation.label} Cost - {self.account_id} - {location.description}",
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
        )
        _LOGGER.debug("last_stat for %s: %s", aggregation.label, last_stat)

        smarthub_data = {}
        if not last_stat:
            _LOGGER.debug("Updating %s statistic for the first time", aggregation.label)
            consumption_sum = 0.0
            return_sum      = 0.0
            cost_sum        = 0.0
            last_stats_time = None
            cost_last_stats_time = None

            # Initialize with last HISTORICAL_IMPORT_DAYS (usually 90) days of data
            start_datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=HISTORICAL_IMPORT_DAYS)

            # Load read data for use in populating statistics
            smarthub_data = await self.api.get_energy_data(location=location, aggregation=aggregation, start_datetime=start_datetime)
        else:
            _LOGGER.debug("Checking if data migration is needed for %s...", aggregation.label)
            migrated = False
            # SmartHub doesn't hvae any current migrations - this sample code was left
            # from the opower version
            #migrated = await self._async_maybe_migrate_statistics(
            #    account.utility_account_id,
            #    {
            #        cost_statistic_id: compensation_statistic_id,
            #        consumption_statistic_id: return_statistic_id,
            #    },
            #    {
            #        cost_statistic_id: cost_metadata,
            #        compensation_statistic_id: compensation_metadata,
            #        consumption_statistic_id: consumption_metadata,
            #        return_metadata: return_metadata,
            #    },
            #)
            if migrated:
                # Skip update to avoid working on old data since the migration is done
                # asynchronously. Update the statistics in the next refresh in 12h.
                _LOGGER.debug(
                    "Statistics migration completed. Skipping update for now"
                )
                return

            # Update reads...
            # Load read data for use in populating statistics
            start_datetime = datetime.fromtimestamp(last_stat[consumption_statistic_id][0]["start"], tz=timezone.utc)

            # always backdate the start_datetime to ensure no gaps in recorded data
            start_datetime = start_datetime - timedelta(days=2)

            _LOGGER.debug("Fetching %s statistics from %s", aggregation.label, start_datetime)
            smarthub_data = await self.api.get_energy_data(location=location, start_datetime=start_datetime, aggregation=aggregation)

            if not smarthub_data or not smarthub_data.get("USAGE"):
              _LOGGER.warning("No data received from SmartHub API for location %s to populate historical %s stats", location, aggregation.label)
              # No new data to record in statatistics
              return

            start = smarthub_data.get("USAGE")[0].get("reading_time")
            _LOGGER.debug("Getting %s statistics at: %s", aggregation.label, start)

            # In the common case there should be a previous statistic at start time
            # so we only need to fetch one statistic. If there isn't any, fetch all.
            # Counterintutitively - but consistent with opower - this aligns the
            # last Stats collection with the data collected form the server - then imports
            # and overrights all the data after that point. The opower logic is that the
            # data might be refreshed, or have collection gaps that are fixed.
            # Its duplicated for SmartHub as it seems reasonable.
            for end in (start + timedelta(seconds=1), None):
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    end,
                    {
                        consumption_statistic_id,
                        return_statistic_id,
                        cost_statistic_id,
                    },
                    aggregation.period,
                    None,
                    {"sum"},
                )
                if stats:
                    break
                if end:
                    _LOGGER.debug(
                        "Not found. Trying to find the oldest statistic after %s",
                        start,
                    )
            # We are in this code path only if get_last_statistics found a stat
            # so statistics_during_period should also have found at least one.
            assert stats

            def _safe_get_sum(records: list[Any]) -> float:
                if records and "sum" in records[0]:
                    return float(records[0]["sum"])
                return 0.0

            consumption_sum = _safe_get_sum(stats.get(consumption_statistic_id, []))
            return_sum    = _safe_get_sum(stats.get(return_statistic_id, []))
            cost_sum      = _safe_get_sum(stats.get(cost_statistic_id, []))
            last_stats_time = stats[consumption_statistic_id][0]["start"]

            # Cost resumes from its own last statistic rather than the
            # consumption one, so enabling a rate on an install that already has
            # consumption history starts cost cleanly instead of inheriting an
            # offset it never accumulated.
            cost_records = stats.get(cost_statistic_id, [])
            cost_last_stats_time = cost_records[0]["start"] if cost_records else None

            _LOGGER.info(f"Updating %s statistics since %s", aggregation.label, last_stats_time)

        consumption_statistics = []
        return_statistics      = []
        cost_statistics        = []

        for cost_read in smarthub_data.get("USAGE", []):
            start = cost_read.get("reading_time")
            consumption_state = max(0, cost_read.get("consumption"))

            if last_stats_time is None or start.timestamp() > last_stats_time:
                consumption_sum += consumption_state

                consumption_statistics.append(
                    StatisticData(
                        start=start, state=consumption_state, sum=consumption_sum
                    )
                )

            # Cost is filtered on its own last-statistic time so it can backfill
            # independently of consumption over the fetched window.
            if track_cost and (
                cost_last_stats_time is None or start.timestamp() > cost_last_stats_time
            ):
                cost_state = consumption_state * rate if rate is not None else 0.0
                if base_charge is not None:
                    # Fixed monthly charge, spread across this month's periods.
                    cost_state += base_charge * _period_share_of_month(start, aggregation)
                cost_sum += cost_state

                cost_statistics.append(
                    StatisticData(start=start, state=cost_state, sum=cost_sum)
                )

        for return_read in smarthub_data.get("USAGE_RETURN", []):
            start = return_read.get("reading_time")
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue

            return_state = max(0, return_read.get("consumption"))
            return_sum += return_state

            return_statistics.append(
                StatisticData(
                    start=start, state=return_state, sum=return_sum
                )
            )

        # If the location description is blank, use the meter name instead.
        if location.description == "":
          consumption_metadata["name"]=f"{location.provider} SmartHub {industry_label} {aggregation.label} Usage - {self.account_id} - {smarthub_data.get(METER_NAME, None)}"
          return_metadata["name"]=f"{location.provider} SmartHub {industry_label} {aggregation.label} Return - {self.account_id} - {smarthub_data.get(METER_NAME, None)}"
          cost_metadata["name"]=f"{location.provider} SmartHub {industry_label} {aggregation.label} Cost - {self.account_id} - {smarthub_data.get(METER_NAME, None)}"

        _LOGGER.info(
            "Adding %s statistics for %s",
            len(consumption_statistics),
            consumption_statistic_id,
        )
        async_add_external_statistics(
            self.hass, consumption_metadata, consumption_statistics
        )

        if "USAGE_RETURN" in smarthub_data:
          _LOGGER.info(
            "Adding %s return statistics for %s",
            len(return_statistics),
            return_statistic_id,
          )
          async_add_external_statistics(
            self.hass, return_metadata, return_statistics
          )

        if track_cost:
          _LOGGER.info(
            "Adding %s cost statistics for %s (rate %s, monthly base charge %s)",
            len(cost_statistics),
            cost_statistic_id,
            rate,
            base_charge,
          )
          async_add_external_statistics(
            self.hass, cost_metadata, cost_statistics
          )


class SmartHubUsageSensor(CoordinatorEntity, SensorEntity):
    """Representation of a SmartHub usage sensor (electric or water)."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: SmartHubDataUpdateCoordinator,
        config_entry: ConfigEntry,
        config: Dict[str, Any],
        location: SmartHubLocation,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        spec = INDUSTRY_SENSOR_SPEC[location.industry]
        self._attr_device_class = spec["device_class"]
        self._attr_native_unit_of_measurement = spec["unit"]
        self._attr_icon = spec["icon"]

        self._config_entry = config_entry
        self._config = config
        self._data_key = _location_data_key(location)
        self._attr_unique_id = f"{config_entry.unique_id}_{location.id}_{location.industry.lower()}"
        self.location = location

        # Extract account info for naming
        account_id = config.get("account_id", "Unknown")
        industry_label = INDUSTRY_LABEL[location.industry]

        self._attr_name = f"{self.location.provider} SmartHub {industry_label} Monthly Usage - {account_id} {self.location.description}"

        _LOGGER.debug("Initialized SmartHub %s sensor with unique_id: %s", location.industry, self._attr_unique_id)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None

        value = self.coordinator.data.get(self._data_key, {}).get(USAGE_SENSOR_KEY, None)
        if value is None:
            _LOGGER.debug("No usage value found in coordinator data")
            return None

        try:
            return float(value)
        except (ValueError, TypeError) as e:
            _LOGGER.warning("Could not convert usage value '%s' to float: %s", value, e)
            return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {
            ATTR_ACCOUNT_ID: self._config.get("account_id"),
            ATTR_LOCATION_ID: self.location.id,
            ATTR_INDUSTRY: self.location.industry,
        }

        # Add last reading time & meter name if available
        if self.coordinator.data:
            location_data = self.coordinator.data.get(self._data_key, {})

            last_reading = location_data.get(ATTR_LAST_READING_TIME)
            if last_reading:
                attributes[ATTR_LAST_READING_TIME] = last_reading

            meter_name = location_data.get(METER_NAME)
            if meter_name:
                attributes[METER_NAME] = meter_name

        return attributes

    @property
    def device_info(self) -> Dict[str, Any]:
        """Return device information."""
        account_id = self._config.get("account_id", "Unknown")
        host = self._config.get("host", "Unknown")
        industry_label = INDUSTRY_LABEL[self.location.industry]

        return {
            "identifiers": {(DOMAIN, f"{self._config_entry.unique_id or self._config_entry.entry_id}_{self.location.id}_{self.location.industry.lower()}")},
            "name": f"{self.location.provider} SmartHub {industry_label} Monthly Usage ({account_id} - {self.location.description})",
            "manufacturer": "SmartHub Coop",
            "model": f"{industry_label} Monitor",
            "configuration_url": f"https://{host}",
        }
