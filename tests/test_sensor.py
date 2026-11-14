"""Tests for Thames Water sensor statistics generation."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
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

from custom_components.thames_water.const import DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN
from custom_components.thames_water.sensor import (
    CONSUMPTION_STATISTIC_ID,
    COST_STATISTIC_ID,
    INITIAL_HISTORY,
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
        assert stats[0]["sum"] == 100

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


@contextmanager
def _recorder(newest_cost: float | None, history: list[dict]):
    instance = MagicMock()

    async def run(func, *args):
        return func(*args)

    instance.async_add_executor_job = run
    if newest_cost is None:
        last = {}
    else:
        last = {
            COST_STATISTIC_ID: [
                {
                    "start": datetime(2026, 10, 1, 0, tzinfo=LONDON_TZ).timestamp(),
                    "sum": newest_cost,
                }
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
            return_value={CONSUMPTION_STATISTIC_ID: history},
        ),
    ):
        yield


class TestCostStatistic:
    async def _run(self, coordinator, newest_cost, history, fetched):
        with (
            _recorder(newest_cost, history),
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
                coordinator, "_async_start_day", return_value=date(2026, 10, 25)
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

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_coordinator(tariff: Tariff | None = None) -> ThamesWaterCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"username": "u", "password": "p", "account_number": "1", "meter_id": "m"},
    )
    return ThamesWaterCoordinator(_FakeHass(), entry, MagicMock(data=tariff))


@contextmanager
def _recorder_with_newest(newest: datetime | None):
    """Patch the recorder lookup of the newest stored consumption statistic."""
    rows = (
        {}
        if newest is None
        else {CONSUMPTION_STATISTIC_ID: [{"start": newest.timestamp()}]}
    )
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
    ):
        yield


@pytest.fixture(autouse=True)
def no_statistics_yet():
    """By default nothing has been recorded, as on a first setup."""
    with _recorder_with_newest(None):
        yield


class TestUpdate:
    async def test_only_hourly_usage_is_fetched(self) -> None:
        """Each cycle makes a single hourly request and no daily one."""
        coordinator = _make_coordinator()
        usage = _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            patch(f"{SENSOR}.ThamesWater") as thames_water,
            patch.object(coordinator, "_inject_statistics") as inject,
        ):
            thames_water.return_value.get_meter_usage.return_value = usage
            await coordinator._async_update_data()

        calls = thames_water.return_value.get_meter_usage.call_args_list
        assert [call.kwargs["granularity"] for call in calls] == ["H"]
        assert [call.args[0] for call in inject.call_args_list] == [
            "thames_water:thameswater_consumption_hourly",
            "thames_water:thameswater_consumption",
        ]

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
    async def _fetch_window(self, newest: datetime | None) -> dict[str, date]:
        coordinator = _make_coordinator()
        captured: dict[str, date] = {}

        def fake_fetch(start, end):
            captured["start"], captured["end"] = start, end
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")]), None

        with (
            _recorder_with_newest(newest),
            patch.object(coordinator, "_fetch", side_effect=fake_fetch),
            patch.object(coordinator, "_inject_statistics"),
        ):
            await coordinator._async_update_data()
        return captured

    async def test_starts_on_the_day_after_the_newest_statistic_and_ends_today(
        self,
    ) -> None:
        window = await self._fetch_window(datetime(2026, 10, 20, 13, tzinfo=LONDON_TZ))

        assert window["start"] == date(2026, 10, 21)
        assert window["end"] == datetime.now(LONDON_TZ).date()

    async def test_a_statistic_just_after_local_midnight_is_that_london_day(
        self,
    ) -> None:
        """00:00 BST is the previous day in UTC but the same day in London."""
        window = await self._fetch_window(datetime(2026, 7, 2, 0, tzinfo=LONDON_TZ))

        assert window["start"] == date(2026, 7, 3)

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
            datetime(2026, 10, 21, 0, tzinfo=LONDON_TZ),
            datetime(2026, 10, 21, 1, tzinfo=LONDON_TZ),
        ]


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
    await hass.async_block_till_done()
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

    async def test_a_failed_sign_in_makes_both_unavailable(
        self, hass: HomeAssistant, client: MagicMock
    ) -> None:
        client.authenticate.side_effect = AuthenticationError("wrong password")
        await _setup(hass)

        assert hass.states.get(CONSUMPTION).state == STATE_UNAVAILABLE
        assert hass.states.get(BALANCE).state == STATE_UNAVAILABLE

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
            hass, dt_util.utcnow() + timedelta(hours=DEFAULT_UPDATE_INTERVAL_HOURS)
        )
        await hass.async_block_till_done(wait_background_tasks=True)

        assert client.get_meter_usage.call_count == 2
