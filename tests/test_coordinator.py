"""Test file for SmartHub sensor (statistics)"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from collections.abc import Generator
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthub import async_setup_entry
from custom_components.smarthub.api import SmartHubAPI, SmartHubAPIError, SmartHubLocation
from custom_components.smarthub.const import DOMAIN, INDUSTRY_ELECTRIC

from custom_components.smarthub.sensor import SmartHubDataUpdateCoordinator
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from datetime import timedelta
from homeassistant.util import dt as dt_util


from homeassistant.components.recorder import get_instance

@pytest.fixture(autouse=True)
def mock_smarthub_api(hass) -> Generator[AsyncMock]:
    """Mock the config entry ..."""

    api = SmartHubAPI(
        email="test@example.com",
        password="testpass",
        account_id="123456",
        timezone="UTC",
        mfa_totp="",
        host="test.smarthub.coop"
    )

    with patch(
        "custom_components.smarthub.api.SmartHubAPI", autospec=True
    ) as mock_api:

        mock_api.timezone="UTC"
        mock_api.parse_usage = api.parse_usage
        mock_api.get_service_locations.return_value = []
        mock_api.get_energy_data.return_value = {}
        yield mock_api


@pytest.fixture()
def mock_config_entry(hass) -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="SmartHub Test",
        data={
            "email": "test@example.com",
            "password": "testpass",
            "account_id": "123456",
            "location_id": "789012",
            "host": "test.smarthub.coop",
            "poll_interval": 60,
            "timezone": "UTC",
            "mfa_totp": "",
        },
        unique_id="test.smarthub.coop_123456",
    )

async def test_coordinator_first_run_forward_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Test the coordinator on its first run with no existing statistics."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]

    test_data = {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND91111111', 'flowDirection': 'FORWARD', 'isNetMeter': False}, # Forward meter is full consumption.
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND91111111", "name": "1ND91111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                            ]
                        },
                    ]
                }
            ]
        }
    }

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(test_data, INDUSTRY_ELECTRIC)

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    # Check stats for electric account '111111'
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # The first hour's statistics summary is...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 111.0


async def test_coordinator_first_run_net_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Test the coordinator on its first run with no existing statistics."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]

    test_data = {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND81111111', 'seriesId': '1ND81111111', 'flowDirection': 'NET', 'isNetMeter': True}, # Includes in and out bound flows (posirive/negative)
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND91111111', 'flowDirection': 'FORWARD', 'isNetMeter': False}, # Forward meter is full consumption.
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND91111111", "name": "1ND91111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                                {"x": 1762219800000, "y": 1},
                            ]
                        },
                        {
                            "meterNumber": "1ND81111111", "name": "1ND81111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  -5}, # generated 15 KW of power this hour - returned 5 to the grid
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                                {"x": 1762219800000, "y":  -1}, # generated 2 KW of power this hour - returned 1 to the grid
                            ]
                        },
                    ]
                }
            ]
        }
    }

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(test_data, INDUSTRY_ELECTRIC)
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][0]['consumption'] == 5
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][1]['consumption'] == 1

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    # Check stats for electric account '111111'
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
            "smarthub:smarthub_energy_return_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # The first hour's statistics summary is...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 101.0
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][1]["sum"] == 102.0
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][0]["sum"] == 5
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][1]["sum"] == 6

async def test_coordinator_first_run_return_meter(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Test the coordinator on its first run with no existing statistics."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]

    test_data = {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND87334444', 'flowDirection': 'RETURN', 'isNetMeter': False}, # Includes in and out bound flows (posirive/negative)
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND86200137', 'flowDirection': 'FORWARD', 'isNetMeter': False}, # Forward meter is full consumption.
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND86200137", "name": "1ND86200137",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                                {"x": 1762219800000, "y": 1},
                            ]
                        },
                        {
                            "meterNumber": "1ND87334444", "name": "1ND87334444",
                            "data": [
                                {"x": 1762215300000, "y":   0},
                                {"x": 1762216200000, "y":  5}, # generated 15 KW of power this hour - returned 5 to the grid
                                {"x": 1762217100000, "y": 0},
                                {"x": 1762218900000, "y": 0},
                                {"x": 1762219800000, "y":  1}, # generated 2 KW of power this hour - returned 1 to the grid
                            ]
                        },
                    ]
                }
            ]
        }
    }

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(test_data, INDUSTRY_ELECTRIC)
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][0]['consumption'] == 5
    assert mock_smarthub_api.get_energy_data.return_value["USAGE_RETURN"][1]['consumption'] == 1

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    # Check stats for electric account '111111'
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
            "smarthub:smarthub_energy_return_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # The first hour's statistics summary is...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 111.0
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][1]["sum"] == 113.0
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][0]["sum"] == 5
    assert stats["smarthub:smarthub_energy_return_sensor_daily_123456_11111"][1]["sum"] == 6



@pytest.fixture()
def mock_config_entry_with_rate(hass) -> MockConfigEntry:
    """Create a mock config entry that has an electric unit rate configured."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="SmartHub Test",
        data={
            "email": "test@example.com",
            "password": "testpass",
            "account_id": "123456",
            "host": "test.smarthub.coop",
            "poll_interval": 60,
            "timezone": "UTC",
            "mfa_totp": "",
            "electric_rate": 0.10,
        },
        unique_id="test.smarthub.coop_123456",
    )


def _forward_meter_test_data() -> dict:
    """Single forward meter, 3 sub-hour reads in one hour plus one in the next."""
    return {
        "data": {
            "ELECTRIC": [
                {
                    "type": "USAGE",
                    "meters": [
                     {'meterNumber': '1ND91111111', 'seriesId': '1ND91111111', 'flowDirection': 'FORWARD', 'isNetMeter': False},
                    ],
                    "series": [
                        {
                            "meterNumber": "1ND91111111", "name": "1ND91111111",
                            "data": [
                                {"x": 1762215300000, "y":   1},
                                {"x": 1762216200000, "y":  10},
                                {"x": 1762217100000, "y": 100},
                                {"x": 1762218900000, "y": 1},
                            ]
                        },
                    ]
                }
            ]
        }
    }


async def test_coordinator_generates_cost_statistics(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry_with_rate: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Cost statistics are derived as consumption * the configured rate."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(
        _forward_meter_test_data(), INDUSTRY_ELECTRIC
    )

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry_with_rate)

    await coordinator._async_update_data()
    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
            "smarthub:smarthub_energy_cost_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    # Consumption is unchanged by the cost feature...
    assert stats["smarthub:smarthub_energy_sensor_daily_123456_11111"][0]["sum"] == 111.0
    # ...and cost tracks it at the configured $0.10/kWh.
    cost = stats["smarthub:smarthub_energy_cost_sensor_daily_123456_11111"]
    assert cost[0]["state"] == pytest.approx(11.1)
    assert cost[0]["sum"] == pytest.approx(11.1)


async def test_coordinator_cost_includes_monthly_base_charge(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_smarthub_api: AsyncMock,
) -> None:
    """A fixed monthly charge is spread across the month's statistic periods."""
    entry = MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="SmartHub Test",
        data={
            "email": "test@example.com",
            "password": "testpass",
            "account_id": "123456",
            "host": "test.smarthub.coop",
            "poll_interval": 60,
            "timezone": "UTC",
            "mfa_totp": "",
            # No unit rate - isolates the fixed-charge contribution.
            "electric_base_charge": 31.0,
        },
        unique_id="test.smarthub.coop_123456",
    )

    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(
        _forward_meter_test_data(), INDUSTRY_ELECTRIC
    )

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=entry)

    await coordinator._async_update_data()
    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {"smarthub:smarthub_energy_cost_sensor_daily_123456_11111"},
        "hour",
        None,
        {"state", "sum"},
    )

    # The readings fall in November (30 days), so each DAILY period carries
    # 31.00 / 30 of the fixed charge and no volumetric component.
    cost = stats["smarthub:smarthub_energy_cost_sensor_daily_123456_11111"]
    assert cost[0]["state"] == pytest.approx(31.0 / 30)


async def test_coordinator_no_cost_statistics_without_rate(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """No cost statistic is produced when no rate is configured."""
    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]

    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(
        _forward_meter_test_data(), INDUSTRY_ELECTRIC
    )

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    await coordinator._async_update_data()
    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {
            "smarthub:smarthub_energy_sensor_daily_123456_11111",
            "smarthub:smarthub_energy_cost_sensor_daily_123456_11111",
        },
        "hour",
        None,
        {"state", "sum"},
    )

    assert "smarthub:smarthub_energy_sensor_daily_123456_11111" in stats
    assert "smarthub:smarthub_energy_cost_sensor_daily_123456_11111" not in stats


async def async_wait_recording_done(hass) -> None:
    """Async wait until recording is done."""
    await hass.async_block_till_done()
    get_instance(hass)._async_commit(dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    await hass.async_block_till_done()


async def test_coordinator_pending_keeps_last_reading(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """A request SmartHub never finishes (get_energy_data -> None) keeps the last value."""
    location = SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
    )
    mock_smarthub_api.get_service_locations.return_value = [location]
    mock_smarthub_api.get_energy_data.return_value = None

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    from custom_components.smarthub.sensor import _location_data_key
    key = _location_data_key(location)
    previous = {"usage": 123.0, "location": location}
    coordinator.data = {key: previous}

    result = await coordinator._async_update_data()

    assert result[key] is previous


async def test_coordinator_pending_without_previous_fails_cleanly(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """With nothing to keep, an unfinished request fails the update without an AttributeError."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    mock_smarthub_api.get_service_locations.return_value = [
      SmartHubLocation(
        id="11111",
        service=INDUSTRY_ELECTRIC.lower(),
        description="test location",
        provider="test provider",
        industry=INDUSTRY_ELECTRIC,
      )
    ]
    mock_smarthub_api.get_energy_data.return_value = None

    coordinator = SmartHubDataUpdateCoordinator(hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry)

    with pytest.raises(UpdateFailed, match="did not finish"):
        await coordinator._async_update_data()
