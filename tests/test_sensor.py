"""Tests for Thames Water sensor statistics generation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from thameswaterapi import Line, MeterUsage

from custom_components.thames_water import sensor as sensor_module
from custom_components.thames_water.sensor import (
    ThamesWaterSensor,
    _generate_daily_statistics_from_meter_usage,
    _generate_hourly_statistics_from_meter_usage,
    _still_in_force,
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


class TestGenerateDailyStatistics:
    def test_empty_lines(self) -> None:
        meter_usage = _make_meter_usage([])
        stats = _generate_daily_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert stats == []

    def test_single_line(self) -> None:
        meter_usage = _make_meter_usage([_make_line(100.0, 1000.0)])
        stats = _generate_daily_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ)
        assert stats[0]["state"] == 100
        assert stats[0]["sum"] == 1000

    def test_multiple_lines(self) -> None:
        meter_usage = _make_meter_usage(
            [
                _make_line(100.0, 1000.0),
                _make_line(150.0, 1150.0),
                _make_line(80.0, 1230.0),
            ]
        )
        stats = _generate_daily_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert len(stats) == 3
        assert stats[0]["start"] == datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ)
        assert stats[1]["start"] == datetime(2024, 1, 2, 0, 0, tzinfo=LONDON_TZ)
        assert stats[2]["start"] == datetime(2024, 1, 3, 0, 0, tzinfo=LONDON_TZ)
        assert stats[0]["state"] == 100
        assert stats[1]["state"] == 150
        assert stats[2]["state"] == 80

    def test_datetime_start_strips_time(self) -> None:
        meter_usage = _make_meter_usage([_make_line(100.0, 1000.0)])
        stats = _generate_daily_statistics_from_meter_usage(
            datetime(2024, 1, 1, 15, 30, 45), meter_usage
        )
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ)

    def test_timestamps_are_timezone_aware(self) -> None:
        meter_usage = _make_meter_usage([_make_line(100.0, 1000.0)])
        stats = _generate_daily_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert stats[0]["start"].tzinfo is not None

    def test_usage_values_are_truncated_to_int(self) -> None:
        meter_usage = _make_meter_usage([_make_line(99.7, 1000.3)])
        stats = _generate_daily_statistics_from_meter_usage(
            date(2024, 1, 1), meter_usage
        )
        assert stats[0]["state"] == 99
        assert stats[0]["sum"] == 1000


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


def _frozen_date(today: date) -> type:
    """Return a `date` stand-in whose `today()` returns the given day.

    Sensor code uses the module-level `date` binding for two things:
    constructing dates (`date(y, m, d)`) and calling `date.today()`. This
    helper preserves the former and freezes the latter.
    """

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return today

    return _FrozenDate


class _FakeHass:
    """Minimal stand-in for HomeAssistant that runs executor jobs inline."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _make_sensor() -> ThamesWaterSensor:
    return ThamesWaterSensor(
        hass=_FakeHass(),
        name="Test",
        username="u",
        password="p",
        account_number="1",
        meter_id="m",
        unique_id="uid",
    )


class TestDailyFetchThrottling:
    """The daily meter usage endpoint should be hit at most once per day."""

    async def _run_update(
        self, sensor: ThamesWaterSensor, today: date, fetch_calls: list[str]
    ) -> None:
        def fake_fetch(start_dt, end_dt, granularity="H"):
            fetch_calls.append(granularity)
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics"),
            patch.object(sensor_module, "date", _frozen_date(today)),
        ):
            await sensor.async_update()

    async def test_daily_fetched_once_per_day(self) -> None:
        sensor = _make_sensor()
        calls: list[str] = []
        today = date(2024, 6, 1)

        await self._run_update(sensor, today, calls)
        await self._run_update(sensor, today, calls)

        assert calls.count("D") == 1
        assert calls.count("H") == 2

    async def test_daily_fetched_again_on_new_day(self) -> None:
        sensor = _make_sensor()
        calls: list[str] = []

        await self._run_update(sensor, date(2024, 6, 1), calls)
        await self._run_update(sensor, date(2024, 6, 2), calls)

        assert calls.count("D") == 2
        assert calls.count("H") == 2

    async def test_daily_retried_when_previous_fetch_returned_no_data(self) -> None:
        """A failed/empty daily fetch must not suppress the next attempt."""
        sensor = _make_sensor()
        calls: list[str] = []
        today = date(2024, 6, 1)

        def fake_fetch_empty_daily(start_dt, end_dt, granularity="H"):
            calls.append(granularity)
            if granularity == "D":
                return _make_meter_usage([])
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            patch.object(
                sensor, "_fetch_meter_usage", side_effect=fake_fetch_empty_daily
            ),
            patch.object(sensor, "_inject_statistics"),
            patch.object(sensor_module, "date", _frozen_date(today)),
        ):
            await sensor.async_update()
            await sensor.async_update()

        assert calls.count("D") == 2

    async def test_last_reading_time_tracks_latest_hourly_line(self) -> None:
        """The last_reading_time attribute reflects the real hour of the latest reading."""
        sensor = _make_sensor()
        today = date(2024, 6, 10)
        captured: dict[str, datetime] = {}

        def fake_fetch(start_dt, end_dt, granularity="H"):
            if granularity == "H":
                captured["start"] = start_dt
                return _make_meter_usage(
                    [
                        _make_line(1.0, 100.0, "0:00"),
                        _make_line(2.0, 102.0, "1:00"),
                        _make_line(3.0, 105.0, "2:00"),
                    ]
                )
            return _make_meter_usage([_make_line(10.0, 1000.0)])

        with (
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics"),
            patch.object(sensor_module, "date", _frozen_date(today)),
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

    async def test_last_reading_time_falls_back_to_daily(self) -> None:
        """With no hourly data, last_reading_time comes from the daily series."""
        sensor = _make_sensor()
        today = date(2024, 6, 10)
        captured: dict[str, datetime] = {}

        def fake_fetch(start_dt, end_dt, granularity="H"):
            if granularity == "H":
                return _make_meter_usage([])
            captured["start"] = start_dt
            return _make_meter_usage(
                [
                    _make_line(10.0, 1000.0),
                    _make_line(12.0, 1012.0),
                ]
            )

        with (
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics"),
            patch.object(sensor_module, "date", _frozen_date(today)),
        ):
            await sensor.async_update()

        start_day = captured["start"].date()
        expected_last = datetime(
            start_day.year, start_day.month, start_day.day, 0, 0, tzinfo=LONDON_TZ
        ) + timedelta(days=1)
        assert sensor.extra_state_attributes == {
            "last_reading_time": expected_last.isoformat(),
        }

    async def test_daily_retried_when_previous_fetch_raised(self) -> None:
        sensor = _make_sensor()
        calls: list[str] = []
        today = date(2024, 6, 1)
        raise_once = {"D": True}

        def fake_fetch_flaky_daily(start_dt, end_dt, granularity="H"):
            calls.append(granularity)
            if granularity == "D" and raise_once["D"]:
                raise_once["D"] = False
                raise RuntimeError("boom")
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            patch.object(
                sensor, "_fetch_meter_usage", side_effect=fake_fetch_flaky_daily
            ),
            patch.object(sensor, "_inject_statistics"),
            patch.object(sensor_module, "date", _frozen_date(today)),
        ):
            await sensor.async_update()
            await sensor.async_update()

        assert calls.count("D") == 2
