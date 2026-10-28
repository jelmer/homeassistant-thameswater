"""Tests for Thames Water sensor statistics generation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from thameswaterapi import Line, MeterUsage

from custom_components.thames_water.sensor import (
    CONSUMPTION_STATISTIC_ID,
    INITIAL_HISTORY,
    ThamesWaterSensor,
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
        sensor = _make_sensor()
        calls: list[str] = []

        def fake_fetch(start, end, granularity="H"):
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
        captured: dict[str, date] = {}

        def fake_fetch(start, end, granularity="H"):
            captured["start"] = start
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

        expected_day = captured["start"]
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


class TestResume:
    async def _fetch_window(self, newest: datetime | None) -> dict[str, date]:
        sensor = _make_sensor()
        captured: dict[str, date] = {}

        def fake_fetch(start, end, granularity="H"):
            captured["start"], captured["end"] = start, end
            return _make_meter_usage([_make_line(1.0, 1.0, "0:00")])

        with (
            _recorder_with_newest(newest),
            patch.object(sensor, "_fetch_meter_usage", side_effect=fake_fetch),
            patch.object(sensor, "_inject_statistics"),
        ):
            await sensor.async_update()
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
        sensor = _make_sensor()
        usage = _make_meter_usage(
            [_make_line(1.0, 100.0, "0:00"), _make_line(2.0, 102.0, "1:00")]
        )
        with (
            _recorder_with_newest(datetime(2026, 10, 20, 5, tzinfo=LONDON_TZ)),
            patch.object(sensor, "_fetch_meter_usage", return_value=usage),
            patch.object(sensor, "_inject_statistics") as inject,
        ):
            await sensor.async_update()

        stats = inject.call_args_list[0].args[2]
        assert [row["start"] for row in stats] == [
            datetime(2026, 10, 21, 0, tzinfo=LONDON_TZ),
            datetime(2026, 10, 21, 1, tzinfo=LONDON_TZ),
        ]
