"""Tests for the history backfill and the daily re-import double count."""
import pytest
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.smarthub.api import Aggregation, SmartHubAPI, SmartHubLocation
from custom_components.smarthub.const import DOMAIN, INDUSTRY_WATER
from custom_components.smarthub.sensor import SmartHubDataUpdateCoordinator

DAILY_ID = "smarthub:smarthub_water_sensor_daily_123456_22222"
HOURLY_ID = "smarthub:smarthub_water_sensor_123456_22222"

WATER_LOCATION = SmartHubLocation(
    id="22222",
    service="water",
    description="SPR",
    provider="test provider",
    industry=INDUSTRY_WATER,
)


@pytest.fixture(autouse=True)
def mock_smarthub_api(hass) -> Generator[AsyncMock]:
    api = SmartHubAPI(
        email="test@example.com",
        password="testpass",
        account_id="123456",
        timezone="UTC",
        mfa_totp="",
        host="test.smarthub.coop",
    )
    with patch("custom_components.smarthub.api.SmartHubAPI", autospec=True) as mock_api:
        mock_api.timezone = "UTC"
        mock_api.parse_usage = api.parse_usage
        mock_api.get_service_locations.return_value = [WATER_LOCATION]
        mock_api.get_energy_data.return_value = {}
        yield mock_api


@pytest.fixture()
def mock_config_entry(hass) -> MockConfigEntry:
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
        },
        unique_id="test.smarthub.coop_123456",
    )


def _water(points: list[tuple[datetime, float]]) -> dict:
    """A SmartHub WATER response with one forward meter."""
    return {
        "data": {
            "WATER": [
                {
                    "type": "USAGE",
                    "meters": [{"meterNumber": "B1", "seriesId": "B1", "flowDirection": "FORWARD"}],
                    "series": [
                        {
                            "meterNumber": "B1",
                            "name": "B1",
                            "data": [{"x": int(t.timestamp() * 1000), "y": y} for t, y in points],
                        }
                    ],
                }
            ]
        }
    }


async def _sums(hass, statistic_id: str) -> list[tuple[float, float]]:
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [(row["state"], row["sum"]) for row in stats.get(statistic_id, [])]


def test_dedupe_reads_keeps_one_per_time_in_order():
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=1)
    reads = [
        {"reading_time": t1, "consumption": 5},
        {"reading_time": t0, "consumption": 1},
        {"reading_time": t1, "consumption": 7},
    ]
    out = SmartHubDataUpdateCoordinator._dedupe_reads(reads)
    assert [r["reading_time"] for r in out] == [t0, t1]
    assert out[1]["consumption"] == 7


async def test_daily_reimport_at_utc_midnight_does_not_double_count(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Daily water readings are stamped at UTC midnight.

    The test instance runs in US/Pacific, so a local-day lookup puts each
    reading in the previous day's bucket - the condition that made every poll
    add the first day of its window into the sum again.
    """
    assert hass.config.time_zone != "UTC"
    day = datetime(2026, 9, 1, tzinfo=timezone.utc)
    points = [(day, 780), (day + timedelta(days=1), 0), (day + timedelta(days=2), 840)]
    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(_water(points), INDUSTRY_WATER)

    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    for _ in range(4):  # several polls over the same overlapping window
        await coordinator._insert_statistics(WATER_LOCATION, Aggregation.DAILY)
        await async_wait_recording_done(hass)

    sums = await _sums(hass, DAILY_ID)
    assert sums[-1][1] == 1620.0
    assert [s for _, s in sums] == [780.0, 780.0, 1620.0]


async def test_backfill_fetches_in_chunks_and_rewrites_from_zero(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_smarthub_api: AsyncMock,
) -> None:
    """Backfill reaches past the existing history and re-sums from zero."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    old = (now - timedelta(days=50)).replace(hour=0)
    recent = (now - timedelta(days=3)).replace(hour=0)
    history = [(old, 100.0), (recent, 10.0)]

    # A normal poll first: it only sees the recent reading.
    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(
        _water([(recent, 10.0)]), INDUSTRY_WATER
    )
    coordinator = SmartHubDataUpdateCoordinator(
        hass, api=mock_smarthub_api, update_interval=timedelta(minutes=720), config_entry=mock_config_entry
    )
    await coordinator._insert_statistics(WATER_LOCATION, Aggregation.HOURLY)
    assert [s for _, s in await _sums(hass, HOURLY_ID)] == [10.0]

    # The API serves whatever falls inside each requested window.
    calls = []

    async def windowed(location, aggregation, start_datetime=None, end_datetime=None):
        calls.append((aggregation, start_datetime, end_datetime))
        lo, hi = start_datetime.timestamp(), end_datetime.timestamp()
        inside = [(t, y) for t, y in history if lo <= t.timestamp() <= hi]
        return mock_smarthub_api.parse_usage(_water(inside), INDUSTRY_WATER)

    mock_smarthub_api.get_energy_data.side_effect = windowed
    results = await coordinator.async_backfill(60)

    hourly_calls = [c for c in calls if c[0] is Aggregation.HOURLY]
    assert len(hourly_calls) >= 2, "60 days must be split into several requests"
    assert all(end is not None for _, _, end in hourly_calls)

    assert [s for _, s in await _sums(hass, HOURLY_ID)] == [100.0, 110.0]
    hourly = next(r for r in results if r["aggregation"] == "Hourly")
    assert hourly["periods"] == 2 and hourly["total"] == 110.0

    # A poll after the backfill continues from the rewritten sum.
    mock_smarthub_api.get_energy_data.side_effect = None
    mock_smarthub_api.get_energy_data.return_value = mock_smarthub_api.parse_usage(
        _water([(recent, 10.0), (recent + timedelta(days=1), 5.0)]), INDUSTRY_WATER
    )
    await coordinator._insert_statistics(WATER_LOCATION, Aggregation.HOURLY)
    assert [s for _, s in await _sums(hass, HOURLY_ID)] == [100.0, 110.0, 115.0]
