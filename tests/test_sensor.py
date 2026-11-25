"""Tests for Thames Water sensor statistics generation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.models import StatisticData
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from thameswaterapi import Account, AuthenticationError, Line, MeterUsage, Tariff

from custom_components.thames_water.const import DOMAIN
from custom_components.thames_water.sensor import (
    CONSUMPTION_STATISTIC_ID,
    COST_STATISTIC_ID,
    INITIAL_HISTORY,
    UPDATE_INTERVAL,
    ThamesWaterCoordinator,
    ThamesWaterSensor,
    _generate_hourly_statistics_from_meter_usage,
    _price_readings,
    _still_in_force,
)

LONDON_TZ = ZoneInfo("Europe/London")
SENSOR = "custom_components.thames_water.sensor"


def _make_meter_usage(lines: list[Line]) -> MeterUsage:
    return MeterUsage(
        IsError=False,
        IsDataAvailable=True,
        IsConsumptionAvailable=True,
        TargetUsage=0.0,
        AverageUsage=0.0,
        ActualUsage=0.0,
        MyUsage=None,
        AverageUsagePerPerson=0.0,
        IsMO365Customer=False,
        IsMOPartialCustomer=False,
        IsMOCompleteCustomer=False,
        IsExtraMonthConsumptionMessage=False,
        Lines=lines,
    )


def _make_line(usage: float, read: float, label: str = "") -> Line:
    return Line(
        Label=label,
        Usage=usage,
        Read=read,
        IsEstimated=False,
        MeterSerialNumberHis="",
    )


class TestGenerateHourlyStatistics:
    def test_empty_lines(self) -> None:
        meter_usage = _make_meter_usage([])
        stats = _generate_hourly_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert stats == []

    def test_single_line(self) -> None:
        meter_usage = _make_meter_usage([_make_line(10.0, 100.0, "0:00")])
        stats = _generate_hourly_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ)
        assert stats[0]["state"] == 10
        assert stats[0]["sum"] == 10

    def test_sum_counts_usage_rather_than_the_meter_register(self) -> None:
        meter_usage = _make_meter_usage(
            [
                _make_line(10.0, 1_117_400.0, "0:00"),
                _make_line(20.0, 1_117_420.0, "1:00"),
            ]
        )
        stats = _generate_hourly_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert [stat["sum"] for stat in stats] == [10, 30]

    def test_sum_continues_from_the_running_total(self) -> None:
        meter_usage = _make_meter_usage(
            [
                _make_line(10.0, 1_117_400.0, "0:00"),
                _make_line(20.0, 1_117_420.0, "1:00"),
            ]
        )
        stats = _generate_hourly_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage, running_total=500.0
        )
        assert [stat["sum"] for stat in stats] == [510, 530]

    def test_multiple_lines(self) -> None:
        meter_usage = _make_meter_usage(
            [
                _make_line(10.0, 100.0, "0:00"),
                _make_line(20.0, 120.0, "1:00"),
                _make_line(5.0, 125.0, "2:00"),
            ]
        )
        stats = _generate_hourly_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert len(stats) == 3
        assert stats[0]["start"] == datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ)
        assert stats[1]["start"] == datetime(2024, 1, 1, 1, 0, tzinfo=LONDON_TZ)
        assert stats[2]["start"] == datetime(2024, 1, 1, 2, 0, tzinfo=LONDON_TZ)
        assert stats[0]["state"] == 10
        assert stats[1]["state"] == 20
        assert stats[2]["state"] == 5

    def test_datetime_start_uses_its_date(self) -> None:
        # Only the date of the start argument is used; the hour comes from
        # the line's own label.
        meter_usage = _make_meter_usage([_make_line(10.0, 100.0, "12:00")])
        stats = _generate_hourly_statistics_from_meter_usage(
            datetime(2024, 1, 1, 6, 0), meter_usage
        )
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(2024, 1, 1, 12, 0, tzinfo=LONDON_TZ)

    def test_a_missing_hour_does_not_shift_the_rest(self) -> None:
        # The spring-forward day has 23 labels, with 01:00 absent, so the
        # hours after a gap have to come from the label rather than the
        # position.
        meter_usage = _make_meter_usage(
            [
                _make_line(10.0, 100.0, "0:00"),
                _make_line(5.0, 125.0, "14:00"),
            ]
        )
        stats = _generate_hourly_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert [stat["start"] for stat in stats] == [
            datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ),
            datetime(2024, 1, 1, 14, 0, tzinfo=LONDON_TZ),
        ]


def _tariff(effective: date = date(2026, 4, 1)) -> Tariff:
    """A tariff whose unit rate is 0.002 GBP per litre."""
    return Tariff(
        clean_water_rate_per_m3=1.0,
        wastewater_rate_per_m3=1.0,
        water_fixed_per_year=0.0,
        wastewater_fixed_per_year=0.0,
        effective_date=effective,
    )


def _reading(day: date, hour: int, litres: float) -> StatisticData:
    return StatisticData(
        start=datetime(day.year, day.month, day.day, hour, tzinfo=LONDON_TZ),
        state=litres,
        sum=litres,
    )


class TestPriceReadings:
    def test_prices_at_the_unit_rate_and_accumulates(self) -> None:
        rows = _price_readings(
            [_reading(date(2026, 10, 1), 0, 100), _reading(date(2026, 10, 1), 1, 50)],
            _tariff(),
            None,
            0.0,
        )
        assert [(row["state"], row["sum"]) for row in rows] == [(0.2, 0.2), (0.1, 0.3)]

    def test_continues_from_the_running_total(self) -> None:
        rows = _price_readings(
            [_reading(date(2026, 10, 1), 0, 100)], _tariff(), None, 10.0
        )
        assert rows[0]["sum"] == 10.2

    def test_a_reading_already_priced_is_not_priced_again(self) -> None:
        first = _reading(date(2026, 10, 1), 0, 100)
        second = _reading(date(2026, 10, 1), 1, 100)
        rows = _price_readings([first, second], _tariff(), first["start"], 0.2)
        assert [row["start"] for row in rows] == [second["start"]]
        assert rows[0]["sum"] == 0.4

    def test_readings_before_the_tariff_took_effect_are_left_unpriced(self) -> None:
        rows = _price_readings(
            [_reading(date(2026, 3, 31), 23, 100), _reading(date(2026, 4, 1), 0, 100)],
            _tariff(),
            None,
            0.0,
        )
        assert [row["start"].date() for row in rows] == [date(2026, 4, 1)]

    def test_readings_after_the_charging_year_wait_for_the_next_tariff(self) -> None:
        rows = _price_readings(
            [_reading(date(2027, 3, 31), 23, 100), _reading(date(2027, 4, 1), 0, 100)],
            _tariff(),
            None,
            0.0,
        )
        assert [row["start"].date() for row in rows] == [date(2027, 3, 31)]


def _stored(rows: dict[str, list[dict]]):
    """Stand in for statistics_during_period, returning rows from the given hour."""

    def during(hass, start, end, statistic_ids, period, units, types):
        return {
            statistic_id: [
                row
                for row in rows.get(statistic_id, [])
                if row["start"] >= start.timestamp()
            ]
            for statistic_id in statistic_ids
        }

    return during


@contextmanager
def _recorder(
    newest_cost: float | None,
    history: list[dict],
    priced: list[dict] | None = None,
    newest_cost_at: datetime = datetime(2026, 10, 1, 0, tzinfo=LONDON_TZ),
):
    instance = MagicMock()

    async def run(func, *args):
        return func(*args)

    instance.async_add_executor_job = run
    if newest_cost is None:
        last = {}
    else:
        last = {
            COST_STATISTIC_ID: [
                {"start": newest_cost_at.timestamp(), "sum": newest_cost}
            ]
        }
    with (
        patch(
            "custom_components.thames_water.sensor.get_instance", return_value=instance
        ),
        patch(
            "custom_components.thames_water.sensor.get_last_statistics",
            return_value=last,
        ),
        patch(
            "custom_components.thames_water.sensor.statistics_during_period",
            side_effect=_stored(
                {CONSUMPTION_STATISTIC_ID: history, COST_STATISTIC_ID: priced or []}
            ),
        ),
    ):
        yield


class TestCostStatistic:
    async def _run(self, coordinator, newest_cost, history, fetched, **stored):
        with (
            _recorder(newest_cost, history, **stored),
            patch(
                "custom_components.thames_water.sensor.async_add_external_statistics"
            ) as add,
        ):
            await coordinator._async_write_cost_statistics(fetched)
        return add

    async def test_first_write_prices_the_history_already_recorded(self) -> None:
        coordinator = _make_coordinator(_tariff())
        history = [
            {
                "start": datetime(2026, 9, 30, 23, tzinfo=LONDON_TZ).timestamp(),
                "state": 100.0,
                "sum": 100.0,
            }
        ]
        fetched = [_reading(date(2026, 10, 1), 0, 50)]

        add = await self._run(coordinator, None, history, fetched)

        metadata, rows = add.call_args.args[1], add.call_args.args[2]
        assert metadata["statistic_id"] == COST_STATISTIC_ID
        assert metadata["unit_of_measurement"] == "GBP"
        assert [row["sum"] for row in rows] == [0.2, 0.3]

    async def test_later_writes_price_only_new_readings(self) -> None:
        coordinator = _make_coordinator(_tariff())
        fetched = [
            _reading(date(2026, 10, 1), 0, 100),
            _reading(date(2026, 10, 1), 1, 100),
        ]

        add = await self._run(coordinator, 0.2, [], fetched)

        rows = add.call_args.args[2]
        assert [row["start"].hour for row in rows] == [1]
        assert rows[0]["sum"] == 0.4

    async def test_recorded_readings_never_priced_are_priced_with_the_new_ones(
        self,
    ) -> None:
        coordinator = _make_coordinator(_tariff())
        history = [
            {
                "start": datetime(2026, 10, 1, hour, tzinfo=LONDON_TZ).timestamp(),
                "state": 100.0,
                "sum": 100.0,
            }
            for hour in (0, 1)
        ]
        fetched = [_reading(date(2026, 10, 1), 2, 100)]

        add = await self._run(coordinator, 0.2, history, fetched)

        rows = add.call_args.args[2]
        assert [row["start"].astimezone(LONDON_TZ).hour for row in rows] == [1, 2]
        assert [row["sum"] for row in rows] == [0.4, 0.6]

    async def test_a_recorded_reading_is_not_priced_again_as_a_fetched_one(
        self,
    ) -> None:
        coordinator = _make_coordinator(_tariff())
        history = [
            {
                "start": datetime(2026, 10, 1, 1, tzinfo=LONDON_TZ).timestamp(),
                "state": 100.0,
                "sum": 100.0,
            }
        ]
        fetched = [_reading(date(2026, 10, 1), 1, 100)]

        add = await self._run(coordinator, 0.2, history, fetched)

        assert [row["sum"] for row in add.call_args.args[2]] == [0.4]

    async def test_readings_fetched_again_are_priced_again(self) -> None:
        coordinator = _make_coordinator(_tariff())
        priced = [
            {
                "start": datetime(2026, 10, 1, 0, tzinfo=LONDON_TZ).timestamp(),
                "state": 0.1,
                "sum": 5.1,
            }
        ]
        fetched = [
            _reading(date(2026, 10, 1), 0, 100),
            _reading(date(2026, 10, 1), 1, 100),
        ]

        add = await self._run(coordinator, None, [], fetched, priced=priced)

        assert [row["sum"] for row in add.call_args.args[2]] == [5.2, 5.4]

    async def test_a_new_charging_year_carries_on_from_the_last_one(self) -> None:
        coordinator = _make_coordinator(_tariff(effective=date(2026, 10, 1)))
        last_hour = datetime(2026, 9, 30, 23, tzinfo=LONDON_TZ)
        priced = [{"start": last_hour.timestamp(), "state": 0.3, "sum": 14.8}]
        fetched = [
            _reading(date(2026, 9, 30), 23, 100),
            _reading(date(2026, 10, 1), 0, 100),
        ]

        add = await self._run(
            coordinator, 14.8, [], fetched, priced=priced, newest_cost_at=last_hour
        )

        rows = add.call_args.args[2]
        assert [row["start"].day for row in rows] == [1]
        assert rows[0]["sum"] == 15.0

    async def test_nothing_is_written_without_a_tariff(self) -> None:
        coordinator = _make_coordinator(None)

        add = await self._run(
            coordinator, None, [], [_reading(date(2026, 10, 1), 0, 50)]
        )

        add.assert_not_called()


class TestUpdateWritesCost:
    async def test_a_refresh_prices_what_it_fetched(self) -> None:
        """Each refresh passes its new readings on to the cost statistic."""
        coordinator = _make_coordinator(_tariff())

        def fake_fetch(start, end):
            return _make_meter_usage(
                [_make_line(100.0, 500.0, "0:00"), _make_line(50.0, 550.0, "1:00")]
            ), None

        with (
            _recorder(None, []),
            patch.object(
                coordinator,
                "_async_resume_point",
                return_value=(date(2026, 10, 25), 0.0),
            ),
            patch.object(coordinator, "_fetch", side_effect=fake_fetch),
            patch.object(coordinator, "_inject_statistics"),
            patch(
                "custom_components.thames_water.sensor.async_add_external_statistics"
            ) as add,
        ):
            await coordinator._async_update_data()

        assert add.call_args.args[1]["statistic_id"] == COST_STATISTIC_ID
        assert [row["sum"] for row in add.call_args.args[2]] == [0.2, 0.3]


class TestStillInForce:
    @staticmethod
    def _charging_year_start(today: date) -> date:
        """The 1 April the charging year containing ``today`` began on."""
        year = today.year if today >= date(today.year, 4, 1) else today.year - 1
        return date(year, 4, 1)

    def test_this_charging_year(self) -> None:
        assert _still_in_force(self._charging_year_start(date.today()))

    def test_the_charging_year_before(self) -> None:
        started = self._charging_year_start(date.today())
        assert not _still_in_force(date(started.year - 1, 4, 1))


class _FakeHass:
    """Minimal stand-in for HomeAssistant that runs executor jobs inline."""

    config_entries = MagicMock()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_coordinator(tariff: Tariff | None = None) -> ThamesWaterCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "u", "password": "p", "account_number": "1", "meter_id": "m"},
    )
    return ThamesWaterCoordinator(_FakeHass(), entry, MagicMock(data=tariff))


@contextmanager
def _recorder_with_newest(
    newest: datetime | None,
    total: float | None = 0.0,
    usage: float = 0.0,
    earlier: list[dict] | None = None,
):
    """Patch the recorder lookups of the stored consumption statistic."""
    rows = (
        {}
        if newest is None
        else {
            CONSUMPTION_STATISTIC_ID: [
                {"start": newest.timestamp(), "state": usage, "sum": total}
            ]
        }
    )
    stored = {
        CONSUMPTION_STATISTIC_ID: (earlier or [])
        + rows.get(CONSUMPTION_STATISTIC_ID, [])
    }
    instance = MagicMock()

    async def run(func, *args):
        return func(*args)

    instance.async_add_executor_job = run
    with (
        patch(
            "custom_components.thames_water.sensor.get_instance", return_value=instance
        ),
        patch(
            "custom_components.thames_water.sensor.get_last_statistics",
            return_value=rows,
        ),
        patch(
            "custom_components.thames_water.sensor.statistics_during_period",
            side_effect=_stored(stored),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def no_statistics_yet():
    """By default nothing has been recorded, as on a first setup."""
    with _recorder_with_newest(None):
        yield


def test_a_stored_interval_from_an_older_version_is_ignored(
    hass: HomeAssistant,
) -> None:
    """Entries created while the interval was an option keep working unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "u",
            "password": "p",
            "account_number": "1",
            "meter_id": "m",
            "update_interval_hours": 1,
        },
    )
    entry.add_to_hass(hass)

    assert (
        ThamesWaterCoordinator(hass, entry, MagicMock()).update_interval
        == UPDATE_INTERVAL
    )


class TestUpdate:
    async def test_only_hourly_usage_is_fetched(self) -> None:
        """Each cycle makes a single hourly request and no daily one."""
        coordinator = _make_coordinator()
        usage = _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            patch.object(coordinator, "_client") as client,
            patch.object(coordinator, "_inject_statistics") as inject,
        ):
            client.get_meter_usage.return_value = usage
            await coordinator._async_update_data()

        calls = client.get_meter_usage.call_args_list
        assert [call.kwargs["granularity"] for call in calls] == ["H"]
        assert [call.args[0] for call in inject.call_args_list] == [
            "thames_water:thameswater_consumption_hourly",
            "thames_water:thameswater_consumption",
        ]

    async def test_overlapping_refreshes_fetch_one_at_a_time(self) -> None:
        """The client is shared, so a requested refresh that overlaps a
        scheduled one waits rather than using it from a second thread."""
        coordinator = _make_coordinator()
        usage = _make_meter_usage([_make_line(1.0, 1.0, "0:00")])
        release = asyncio.Event()
        running = 0
        most_at_once = 0

        async def slow_executor_job(func, *args):
            nonlocal running, most_at_once
            running += 1
            most_at_once = max(most_at_once, running)
            await release.wait()
            running -= 1
            return func(*args)

        coordinator.hass.async_add_executor_job = slow_executor_job
        with (
            patch.object(coordinator, "_fetch", return_value=(usage, None)),
            patch.object(coordinator, "_inject_statistics"),
        ):
            refreshes = [
                asyncio.ensure_future(coordinator._async_update_data())
                for _ in range(2)
            ]
            for _ in range(10):
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*refreshes)

        assert most_at_once == 1

    async def test_cancelled_refresh_still_serializes_fetch_in_threads(self) -> None:
        """Cancelling a refresh releases the asyncio lock while the worker thread
        is still running; a second refresh must still wait at the thread lock."""
        coordinator = _make_coordinator()
        usage = _make_meter_usage([_make_line(1.0, 1.0, "0:00")])
        coordinator.hass.async_add_executor_job = asyncio.to_thread
        entered = threading.Event()
        second_waiting = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        running = 0
        most_at_once = 0

        real_lock = coordinator._thread_lock
        acquires = 0

        class _ObservableLock:
            def __enter__(self) -> None:
                nonlocal acquires
                with state_lock:
                    acquires += 1
                    if acquires == 2:
                        second_waiting.set()
                real_lock.acquire()

            def __exit__(self, *args: object) -> None:
                real_lock.release()

        coordinator._thread_lock = _ObservableLock()  # type: ignore[assignment]

        def slow_authenticate() -> None:
            nonlocal running, most_at_once
            with state_lock:
                running += 1
                most_at_once = max(most_at_once, running)
                first_call = not entered.is_set()
                if first_call:
                    entered.set()
            if first_call:
                assert release.wait(timeout=5)
            with state_lock:
                running -= 1

        with (
            patch.object(coordinator, "_client") as client,
            patch.object(coordinator, "_inject_statistics"),
        ):
            client.authenticate.side_effect = slow_authenticate
            client.get_meter_usage.return_value = usage
            client.get_account.return_value = None

            first = asyncio.create_task(coordinator._async_update_data())
            assert await asyncio.to_thread(entered.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            second = asyncio.create_task(coordinator._async_update_data())
            assert await asyncio.to_thread(second_waiting.wait, 5)
            assert most_at_once == 1
            release.set()
            await second

        assert most_at_once == 1
        assert client.authenticate.call_count == 2

    async def test_last_reading_time_tracks_latest_hourly_line(self) -> None:
        """The last_reading_time attribute reflects the real hour of the latest reading."""
        coordinator = _make_coordinator()
        captured: dict[str, date] = {}

        def fake_fetch(start, end):
            captured["start"] = start
            return _make_meter_usage(
                [
                    _make_line(1.0, 100.0, "0:00"),
                    _make_line(2.0, 102.0, "1:00"),
                    _make_line(3.0, 105.0, "2:00"),
                ]
            ), None

        with (
            patch.object(coordinator, "_fetch", side_effect=fake_fetch),
            patch.object(coordinator, "_inject_statistics"),
        ):
            coordinator.data = await coordinator._async_update_data()

        expected_day = captured["start"]
        sensor = ThamesWaterSensor(coordinator, "Test", "uid")
        assert sensor.state == 105.0
        assert sensor.extra_state_attributes == {
            "last_reading_time": datetime(
                expected_day.year,
                expected_day.month,
                expected_day.day,
                2,
                0,
                tzinfo=LONDON_TZ,
            ).isoformat(),
        }

    async def test_a_refresh_with_nothing_new_keeps_the_last_reading(self) -> None:
        coordinator = _make_coordinator()
        fetched = [
            (_make_meter_usage([_make_line(1.0, 100.0, "0:00")]), None),
            (_make_meter_usage([]), None),
            (None, None),
        ]

        with (
            patch.object(coordinator, "_fetch", side_effect=fetched),
            patch.object(coordinator, "_inject_statistics"),
        ):
            first = coordinator.data = await coordinator._async_update_data()
            assert first.latest_read == 100.0
            assert await coordinator._async_update_data() == first
            assert await coordinator._async_update_data() == first


class TestResume:
    async def _fetch_window(
        self, newest: datetime | None, earlier: list[dict] | None = None
    ) -> dict[str, date]:
        coordinator = _make_coordinator()
        captured: dict[str, date] = {}

        def fake_fetch(start, end):
            captured["start"], captured["end"] = start, end
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")]), None

        with (
            _recorder_with_newest(newest, earlier=earlier),
            patch.object(coordinator, "_fetch", side_effect=fake_fetch),
            patch.object(coordinator, "_inject_statistics"),
        ):
            await coordinator._async_update_data()
        return captured

    async def test_starts_on_the_day_before_the_newest_statistic_and_ends_today(
        self,
    ) -> None:
        earlier = [
            {
                "start": datetime(2026, 10, 19, 5, tzinfo=LONDON_TZ).timestamp(),
                "state": 1.0,
                "sum": 1.0,
            }
        ]
        window = await self._fetch_window(
            datetime(2026, 10, 20, 13, tzinfo=LONDON_TZ), earlier
        )

        assert window["start"] == date(2026, 10, 19)
        assert window["end"] == datetime.now(LONDON_TZ).date()

    async def test_starts_on_the_newest_day_if_the_day_before_has_nothing(
        self,
    ) -> None:
        window = await self._fetch_window(datetime(2026, 10, 20, 13, tzinfo=LONDON_TZ))

        assert window["start"] == date(2026, 10, 20)

    async def test_a_statistic_just_after_local_midnight_is_that_london_day(
        self,
    ) -> None:
        """00:00 BST is the previous day in UTC but the same day in London."""
        window = await self._fetch_window(datetime(2026, 7, 2, 0, tzinfo=LONDON_TZ))

        assert window["start"] == date(2026, 7, 2)

    async def test_first_setup_looks_back_a_fixed_period(self) -> None:
        window = await self._fetch_window(None)

        assert window["start"] == (datetime.now(LONDON_TZ).date() - INITIAL_HISTORY)

    async def test_statistics_are_dated_from_the_day_resumed_from(self) -> None:
        coordinator = _make_coordinator()
        usage = _make_meter_usage(
            [_make_line(1.0, 100.0, "0:00"), _make_line(2.0, 102.0, "1:00")]
        )
        with (
            _recorder_with_newest(datetime(2026, 10, 20, 5, tzinfo=LONDON_TZ)),
            patch.object(coordinator, "_fetch", return_value=(usage, None)),
            patch.object(coordinator, "_inject_statistics") as inject,
        ):
            await coordinator._async_update_data()

        stats = inject.call_args_list[0].args[2]
        assert [row["start"] for row in stats] == [
            datetime(2026, 10, 20, 0, tzinfo=LONDON_TZ),
            datetime(2026, 10, 20, 1, tzinfo=LONDON_TZ),
        ]

    async def _sums_after_update(
        self, newest: datetime | None, total=None, earlier: list[dict] | None = None
    ):
        coordinator = _make_coordinator()
        usage = _make_meter_usage(
            [
                _make_line(1.0, 1_117_456.0, "0:00"),
                _make_line(2.0, 1_117_458.0, "1:00"),
            ]
        )
        with (
            _recorder_with_newest(newest, total, earlier=earlier),
            patch.object(coordinator, "_fetch", return_value=(usage, None)),
            patch.object(coordinator, "_inject_statistics") as inject,
        ):
            await coordinator._async_update_data()
        return {
            call.args[0]: [row["sum"] for row in call.args[2]]
            for call in inject.call_args_list
        }

    async def test_first_setup_sums_start_from_zero(self) -> None:
        sums = await self._sums_after_update(None)

        assert sums[CONSUMPTION_STATISTIC_ID] == [1.0, 3.0]

    async def test_carries_on_from_the_total_before_the_first_day_fetched(
        self,
    ) -> None:
        earlier = [
            {
                "start": datetime(2026, 10, 19, 0, tzinfo=LONDON_TZ).timestamp(),
                "state": 10.0,
                "sum": 260.0,
            }
        ]
        sums = await self._sums_after_update(
            datetime(2026, 10, 20, 23, tzinfo=LONDON_TZ), total=300.0, earlier=earlier
        )

        assert sums[CONSUMPTION_STATISTIC_ID] == [251.0, 253.0]

    async def test_installs_that_stored_the_register_carry_on_from_it(self) -> None:
        sums = await self._sums_after_update(
            datetime(2026, 10, 20, 23, tzinfo=LONDON_TZ), total=1_117_455.0
        )

        assert sums[CONSUMPTION_STATISTIC_ID] == [1_117_456.0, 1_117_458.0]


class TestImportChosenAtSetup:
    async def _update(
        self,
        import_from: date,
        newest: datetime | None,
        earlier: list[dict] | None = None,
        newest_usage: float = 0.0,
    ) -> tuple[date, list[float]]:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "username": "u",
                "password": "p",
                "account_number": "1",
                "meter_id": "m",
                "import_from": import_from.isoformat(),
            },
        )
        coordinator = ThamesWaterCoordinator(_FakeHass(), entry, MagicMock(data=None))
        usage = _make_meter_usage(
            [_make_line(1.0, 100.0, "0:00"), _make_line(2.0, 102.0, "1:00")]
        )
        with (
            _recorder_with_newest(newest, 300.0, newest_usage, earlier),
            patch.object(coordinator, "_fetch", return_value=(usage, None)) as fetch,
            patch.object(coordinator, "_inject_statistics") as inject,
        ):
            await coordinator._async_update_data()
        sums = [row["sum"] for row in inject.call_args_list[1].args[2]]
        return fetch.call_args.args[0], sums

    async def test_starts_on_the_chosen_day_even_with_newer_statistics(self) -> None:
        start, sums = await self._update(
            date(2026, 1, 1),
            datetime(2026, 10, 20, 23, tzinfo=LONDON_TZ),
            newest_usage=300.0,
        )

        assert start == date(2026, 1, 1)
        assert sums == [1.0, 3.0]

    async def test_carries_on_from_the_total_stored_before_the_chosen_day(
        self,
    ) -> None:
        earlier = [
            {
                "start": datetime(2026, 3, 1, 0, tzinfo=LONDON_TZ).timestamp(),
                "state": 10.0,
                "sum": 260.0,
            }
        ]
        start, sums = await self._update(
            date(2026, 3, 1), datetime(2026, 10, 20, 23, tzinfo=LONDON_TZ), earlier
        )

        assert start == date(2026, 3, 1)
        assert sums == [251.0, 253.0]

    async def test_a_chosen_day_after_the_newest_statistic_leaves_no_gap(
        self,
    ) -> None:
        start, sums = await self._update(
            date(2026, 10, 20), datetime(2026, 10, 10, 23, tzinfo=LONDON_TZ)
        )

        assert start == date(2026, 10, 10)
        assert sums == [301.0, 303.0]

CONSUMPTION = "sensor.thames_water_sensor"
BALANCE = "sensor.thames_water_outstanding_balance"
ENTRY_DATA = {
    "username": "someone@example.com",
    "password": "hunter2",
    "account_number": "900088966994",
    "meter_id": "314287270",
}


@pytest.fixture
def client() -> Generator[MagicMock]:
    """A Thames Water that answers with one reading, for setting an entry up."""
    client = MagicMock()
    client.get_meter_usage.return_value = _make_meter_usage(
        [_make_line(10.0, 66289.0, "23:00")]
    )
    client.get_account.return_value = Account(
        contractAccountNumber=ENTRY_DATA["account_number"],
        paymentDueAmount=12.34,
        currentBalance=-12.34,
        isInCredit=False,
    )
    with (
        _recorder(None, []),
        patch("homeassistant.components.recorder.async_setup", return_value=True),
        patch(f"{SENSOR}.async_add_external_statistics"),
        patch(f"{SENSOR}.get_tariff", return_value=_tariff()),
        patch(f"{SENSOR}.ThamesWater", return_value=client) as thames_water,
    ):
        client.constructed = thames_water
        yield client


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


class TestSetup:
    async def test_one_sign_in_serves_both_sensors(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        await _setup(hass)

        assert client.constructed.call_count == 1
        assert client.authenticate.call_count == 1
        assert hass.states.get(CONSUMPTION).state == "66289.0"
        balance = hass.states.get(BALANCE)
        assert balance.state == "12.34"
        assert balance.attributes["current_balance"] == -12.34
        assert balance.attributes["is_in_credit"] is False

    async def test_later_refreshes_sign_in_on_the_same_client(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        """The client keeps the refresh token, so later sign-ins can use it."""
        await _setup(hass)
        async_fire_time_changed(hass, dt_util.utcnow() + UPDATE_INTERVAL)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert client.constructed.call_count == 1
        assert client.authenticate.call_count == 2

    async def test_unloading_closes_the_session(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        entry = await _setup(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)

        client.close.assert_called_once_with()

    async def test_unloading_during_in_flight_fetch_defers_close(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        """Unloading while a fetch is running in an executor thread does not
        close the client concurrently; the fetch closes it when it finishes."""
        entry = await _setup(hass)
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        def blocking_authenticate() -> None:
            entered.set()
            assert release.wait(timeout=5)

        client.authenticate.side_effect = blocking_authenticate
        client.close.side_effect = closed.set

        async_fire_time_changed(hass, dt_util.utcnow() + UPDATE_INTERVAL)
        assert await hass.async_add_executor_job(entered.wait, 5)

        assert await hass.config_entries.async_unload(entry.entry_id)
        client.close.assert_not_called()

        release.set()
        assert await hass.async_add_executor_job(closed.wait, 5)
        client.close.assert_called_once_with()
        assert client.mock_calls[-1] == call.close()

    async def test_a_failed_usage_fetch_keeps_the_balance(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        client.get_meter_usage.side_effect = RuntimeError("no readings today")
        await _setup(hass)

        assert hass.states.get(CONSUMPTION).state == STATE_UNKNOWN
        assert hass.states.get(BALANCE).state == "12.34"

    async def test_a_failed_account_fetch_keeps_the_reading(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        client.get_account.side_effect = RuntimeError("no account today")
        await _setup(hass)

        assert hass.states.get(CONSUMPTION).state == "66289.0"
        assert hass.states.get(BALANCE).state == STATE_UNKNOWN

    async def test_a_later_failed_account_fetch_keeps_the_last_balance(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        await _setup(hass)
        client.get_account.side_effect = RuntimeError("no account today")
        async_fire_time_changed(
            hass, dt_util.utcnow() + UPDATE_INTERVAL
        )
        await hass.async_block_till_done(wait_background_tasks=True)

        assert hass.states.get(BALANCE).state == "12.34"

    async def test_a_failed_sign_in_makes_both_unavailable(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        client.authenticate.side_effect = AuthenticationError("wrong password")
        await _setup(hass)

        assert hass.states.get(CONSUMPTION).state == STATE_UNAVAILABLE
        assert hass.states.get(BALANCE).state == STATE_UNAVAILABLE

    async def test_the_chosen_history_is_imported_once(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN, data={**ENTRY_DATA, "import_from": "2026-01-01"}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert client.get_meter_usage.call_args.args[1] == date(2026, 1, 1)
        assert "import_from" not in entry.data

        async_fire_time_changed(hass, dt_util.utcnow() + UPDATE_INTERVAL)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert client.get_meter_usage.call_args.args[1] != date(2026, 1, 1)

    async def test_a_rejected_password_asks_for_a_new_one(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        client.authenticate.side_effect = AuthenticationError("wrong password")
        entry = await _setup(hass)

        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert [flow["context"]["source"] for flow in flows] == ["reauth"]
        assert flows[0]["context"]["entry_id"] == entry.entry_id

    async def test_a_failure_to_reach_thames_water_does_not_ask_for_a_password(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        client.authenticate.side_effect = OSError("connection reset")
        await _setup(hass)

        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    async def test_statistics_are_still_fetched_with_the_sensors_disabled(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        registry = er.async_get(hass)
        for unique_id in (
            f"water_usage_{ENTRY_DATA['meter_id']}",
            f"thames_water_balance_{ENTRY_DATA['account_number']}",
        ):
            registry.async_get_or_create(
                "sensor", DOMAIN, unique_id, disabled_by=er.RegistryEntryDisabler.USER
            )
        await _setup(hass)
        assert client.get_meter_usage.call_count == 1

        async_fire_time_changed(
            hass, dt_util.utcnow() + UPDATE_INTERVAL
        )
        await hass.async_block_till_done(wait_background_tasks=True)

        assert client.get_meter_usage.call_count == 2

    async def test_setup_does_not_wait_for_the_first_refresh(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        """Setup registers the entities immediately and refreshes in the background."""
        started = asyncio.Event()
        release = threading.Event()
        finished = False

        def blocked_authenticate():
            nonlocal finished
            hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            finished = True

        client.authenticate.side_effect = blocked_authenticate
        entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
        entry.add_to_hass(hass)
        try:
            assert await hass.config_entries.async_setup(entry.entry_id)
            assert not finished
            assert hass.states.get(CONSUMPTION).state == STATE_UNKNOWN
            assert hass.states.get(BALANCE).state == STATE_UNKNOWN
            await asyncio.wait_for(started.wait(), timeout=5)
            assert not finished
        finally:
            release.set()

        await hass.async_block_till_done(wait_background_tasks=True)
        assert finished
        assert hass.states.get(CONSUMPTION).state == "66289.0"
        assert hass.states.get(BALANCE).state == "12.34"
