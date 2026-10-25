"""Tests for Thames Water sensor statistics generation."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticData
from thameswaterapi import Line, MeterUsage, Tariff

from custom_components.thames_water.sensor import (
    CONSUMPTION_STATISTIC_ID,
    COST_STATISTIC_ID,
    ThamesWaterSensor,
    _generate_hourly_statistics_from_meter_usage,
    _still_in_force,
    price_readings,
)

LONDON_TZ = ZoneInfo("Europe/London")


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
        rows = price_readings(
            [_reading(date(2026, 10, 1), 0, 100), _reading(date(2026, 10, 1), 1, 50)],
            _tariff(),
            None,
            0.0,
        )
        assert [(row["state"], row["sum"]) for row in rows] == [(0.2, 0.2), (0.1, 0.3)]

    def test_continues_from_the_running_total(self) -> None:
        rows = price_readings(
            [_reading(date(2026, 10, 1), 0, 100)], _tariff(), None, 10.0
        )
        assert rows[0]["sum"] == 10.2

    def test_a_reading_already_priced_is_not_priced_again(self) -> None:
        first = _reading(date(2026, 10, 1), 0, 100)
        second = _reading(date(2026, 10, 1), 1, 100)
        rows = price_readings([first, second], _tariff(), first["start"], 0.2)
        assert [row["start"] for row in rows] == [second["start"]]
        assert rows[0]["sum"] == 0.4

    def test_readings_before_the_tariff_took_effect_are_left_unpriced(self) -> None:
        rows = price_readings(
            [_reading(date(2026, 3, 31), 23, 100), _reading(date(2026, 4, 1), 0, 100)],
            _tariff(),
            None,
            0.0,
        )
        assert [row["start"].date() for row in rows] == [date(2026, 4, 1)]

    def test_readings_after_the_charging_year_wait_for_the_next_tariff(self) -> None:
        rows = price_readings(
            [_reading(date(2027, 3, 31), 23, 100), _reading(date(2027, 4, 1), 0, 100)],
            _tariff(),
            None,
            0.0,
        )
        assert [row["start"].date() for row in rows] == [date(2027, 3, 31)]


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
    return (
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
    )


class TestCostStatistic:
    async def _run(self, sensor, newest_cost, history, fetched):
        get_instance_patch, last_patch, during_patch = _recorder(newest_cost, history)
        with (
            get_instance_patch,
            last_patch,
            during_patch,
            patch(
                "custom_components.thames_water.sensor.async_add_external_statistics"
            ) as add,
        ):
            await sensor._async_write_cost_statistics(fetched)
        return add

    async def test_first_write_prices_the_history_already_recorded(self) -> None:
        sensor = _make_sensor(_tariff())
        history = [
            {
                "start": datetime(2026, 9, 30, 23, tzinfo=LONDON_TZ).timestamp(),
                "state": 100.0,
                "sum": 100.0,
            }
        ]
        fetched = [_reading(date(2026, 10, 1), 0, 50)]

        add = await self._run(sensor, None, history, fetched)

        metadata, rows = add.call_args.args[1], add.call_args.args[2]
        assert metadata["statistic_id"] == COST_STATISTIC_ID
        assert metadata["unit_of_measurement"] == "GBP"
        assert [row["sum"] for row in rows] == [0.2, 0.3]

    async def test_later_writes_price_only_new_readings(self) -> None:
        sensor = _make_sensor(_tariff())
        fetched = [
            _reading(date(2026, 10, 1), 0, 100),
            _reading(date(2026, 10, 1), 1, 100),
        ]

        add = await self._run(sensor, 0.2, [], fetched)

        rows = add.call_args.args[2]
        assert [row["start"].hour for row in rows] == [1]
        assert rows[0]["sum"] == 0.4

    async def test_nothing_is_written_without_a_tariff(self) -> None:
        sensor = _make_sensor(None)

        add = await self._run(sensor, None, [], [_reading(date(2026, 10, 1), 0, 50)])

        add.assert_not_called()


class TestUpdateWritesCost:
    async def test_a_refresh_prices_what_it_fetched(self) -> None:
        """Each refresh passes its new readings on to the cost statistic."""
        sensor = _make_sensor(_tariff())
        get_instance_patch, last_patch, during_patch = _recorder(None, [])

        def fake_fetch(start_dt, end_dt, granularity="H"):
            return _make_meter_usage(
                [_make_line(100.0, 500.0, "0:00"), _make_line(50.0, 550.0, "1:00")]
            )

        with (
            get_instance_patch,
            last_patch,
            during_patch,
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics"),
            patch(
                "custom_components.thames_water.sensor.async_add_external_statistics"
            ) as add,
            patch("custom_components.thames_water.sensor.datetime") as fake_datetime,
        ):
            fake_datetime.now.return_value = datetime(2026, 10, 25, 12, 0)
            fake_datetime.side_effect = datetime
            await sensor.async_update()

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


def _make_sensor(tariff: Tariff | None = None) -> ThamesWaterSensor:
    return ThamesWaterSensor(
        hass=_FakeHass(),
        tariff_coordinator=MagicMock(data=tariff),
        name="Test",
        username="u",
        password="p",
        account_number="1",
        meter_id="m",
        unique_id="uid",
    )


class TestUpdate:
    async def test_only_hourly_usage_is_fetched(self) -> None:
        """Each cycle makes a single hourly request and no daily one."""
        sensor = _make_sensor()
        calls: list[str] = []

        def fake_fetch(start_dt, end_dt, granularity="H"):
            calls.append(granularity)
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics") as inject,
        ):
            await sensor.async_update()

        assert calls == ["H"]
        assert [call.args[0] for call in inject.call_args_list] == [
            "thames_water:thameswater_consumption_hourly",
            "thames_water:thameswater_consumption",
        ]

    async def test_last_reading_time_tracks_latest_hourly_line(self) -> None:
        """The last_reading_time attribute reflects the real hour of the latest reading."""
        sensor = _make_sensor()
        captured: dict[str, datetime] = {}

        def fake_fetch(start_dt, end_dt, granularity="H"):
            captured["start"] = start_dt
            return _make_meter_usage(
                [
                    _make_line(1.0, 100.0, "0:00"),
                    _make_line(2.0, 102.0, "1:00"),
                    _make_line(3.0, 105.0, "2:00"),
                ]
            )

        with (
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics"),
        ):
            await sensor.async_update()

        expected_day = captured["start"].date()
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
