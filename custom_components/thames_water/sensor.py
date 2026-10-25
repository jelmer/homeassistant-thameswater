"""Platform for sensor integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Literal
from zoneinfo import ZoneInfo

from thameswaterapi import (
    Account,
    MeterUsage,
    Tariff,
    TariffError,
    ThamesWater,
    get_tariff,
    meter_usage_lines_to_timeseries,
)

from homeassistant.components.recorder import get_instance
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
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN

_LOGGER = logging.getLogger(__name__)
SELENIUM_TIMEOUT = 60

# The tariff is a fixed annual published scheme, so once a day is ample.
TARIFF_SCAN_INTERVAL = timedelta(hours=24)

CONSUMPTION_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption"

LONDON_TZ = ZoneInfo("Europe/London")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> bool:
    """Set up the Thames Water sensor platform."""
    username = entry.data["username"]
    password = entry.data["password"]
    account_number = entry.data["account_number"]
    meter_id = entry.data["meter_id"]

    unique_id = get_unique_id(meter_id)

    _LOGGER.debug(
        "Configured with username: %s, account_number: %s, meter_id: %s",
        username,
        account_number,
        meter_id,
    )

    name = entry.data.get(CONF_NAME, "Thames Water Sensor")

    tariff_coordinator = ThamesWaterTariffCoordinator(hass)
    sensor = ThamesWaterSensor(
        hass,
        tariff_coordinator,
        name,
        username,
        password,
        account_number,
        meter_id,
        unique_id,
    )
    balance_sensor = ThamesWaterBalanceSensor(
        hass,
        username,
        password,
        account_number,
    )
    async_add_entities([sensor, balance_sensor], update_before_add=True)

    update_interval_hours = entry.data.get(
        "update_interval_hours", DEFAULT_UPDATE_INTERVAL_HOURS
    )
    # The callbacks close over the sensors, so an unregistered timer keeps them
    # alive and polling; every reload would stack another one.
    entry.async_on_unload(
        async_track_time_interval(
            hass, sensor.async_update_callback, timedelta(hours=update_interval_hours)
        )
    )
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            balance_sensor.async_update_callback,
            timedelta(hours=update_interval_hours),
        )
    )

    # Tariff sensors. These scrape a public, region-wide page and need no
    # credentials, so they run on their own coordinator. A failure here must not
    # break consumption/balance, so we refresh without raising ConfigEntryNotReady.
    await tariff_coordinator.async_refresh()
    async_add_entities(
        ThamesWaterTariffSensor(tariff_coordinator, description)
        for description in TARIFF_SENSORS
    )
    return True


def get_unique_id(meter_id: str) -> str:
    """Return a unique ID for the sensor."""
    return f"water_usage_{meter_id}"


def _generate_hourly_statistics_from_meter_usage(
    start: date, meter_usage: MeterUsage
) -> list[StatisticData]:
    """Convert hourly meter usage lines into StatisticData entries."""
    return [
        StatisticData(
            start=measurement.hour_start,
            state=measurement.usage,
            sum=measurement.total,
        )
        for measurement in meter_usage_lines_to_timeseries(start, meter_usage.Lines)
    ]


COST_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption_cost"


def price_readings(
    consumption: list[StatisticData],
    tariff: Tariff,
    priced_through: datetime | None,
    running_total: float,
) -> list[StatisticData]:
    """Price hourly readings at the tariff's unit rate.

    Only readings inside the tariff's charging year are priced, so a reading is
    never given a rate that was not in force on its day. A reading already
    priced is never priced again, because cost accumulates while a consumption
    sum is the meter's own odometer.
    """
    lapses = _lapse_date(tariff.effective_date)
    rows: list[StatisticData] = []
    for reading in sorted(consumption, key=lambda row: row["start"]):
        if priced_through is not None and reading["start"] <= priced_through:
            continue
        day = reading["start"].astimezone(LONDON_TZ).date()
        if not tariff.effective_date <= day < lapses:
            continue
        cost = reading["state"] * tariff.unit_rate_per_litre
        running_total += cost
        rows.append(
            StatisticData(
                start=reading["start"],
                state=round(cost, 4),
                sum=round(running_total, 4),
            )
        )
    return rows


class ThamesWaterSensor(SensorEntity):
    """Thames Water Sensor class."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        hass: HomeAssistant,
        tariff_coordinator: ThamesWaterTariffCoordinator,
        name: str,
        username: str,
        password: str,
        account_number: str,
        meter_id: str,
        unique_id: str,
    ) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._tariff_coordinator = tariff_coordinator
        self._name = name
        self._state: float | None = None

        self._username = username
        self._password = password
        self._account_number = account_number
        self._meter_id = meter_id

        self._unique_id = unique_id
        self._attr_should_poll = False
        self._latest_reading_time: datetime | None = None

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this sensor."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self) -> float | None:
        """Return the latest cumulative meter read in litres.

        Thames Water publishes smart meter data with a lag of roughly three
        days, so this is the most recent reading they have made available,
        not a live total. The hour it was recorded on is exposed as the
        ``last_reading_time`` attribute; the full backdated hourly series
        is injected as external statistics (visible in the Energy dashboard
        and in Developer tools > Statistics), not in this entity's history.
        """
        return self._state

    @property
    def unit_of_measurement(self) -> str:
        """Return the unit of measurement (Liters)."""
        return UnitOfVolume.LITERS

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Surface when the current state was actually recorded by the meter.

        The entity's state only moves when the poller fires, but the value
        itself comes from a reading taken ~3 days earlier. Exposing the
        reading's real timestamp makes that lag visible.
        """
        return {
            "last_reading_time": self._latest_reading_time.isoformat()
            if self._latest_reading_time is not None
            else None,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the consumption sensor."""
        return DeviceInfo(
            identifiers={(DOMAIN, "thames_water")},
            manufacturer="Thames Water",
            model="Thames Water",
            name="Thames Water Meter",
        )

    @callback
    async def async_update_callback(self, ts) -> None:
        """Callback triggered by time change to update the sensor and inject statistics."""
        await self.async_update()
        self.async_write_ha_state()

    def _fetch_meter_usage(
        self,
        start_dt: datetime,
        end_dt: datetime,
        granularity: Literal["H", "D", "M"] = "H",
    ) -> MeterUsage:
        """Fetch meter usage from Thames Water API (blocking, run in executor)."""
        thames_water = ThamesWater(
            email=self._username,
            password=self._password,
            account_number=int(self._account_number),
        )
        return thames_water.get_meter_usage(
            self._meter_id, start_dt, end_dt, granularity=granularity
        )

    def _inject_statistics(
        self,
        stat_id: str,
        name: str,
        stats: list[StatisticData],
    ) -> None:
        """Inject external statistics into the recorder."""
        _LOGGER.debug(
            "Injecting %d statistics for %s (first: %s, last: %s)",
            len(stats),
            stat_id,
            stats[0]["start"] if stats else None,
            stats[-1]["start"] if stats else None,
        )
        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.LITERS,
        )
        async_add_external_statistics(self._hass, metadata, stats)

    async def async_update(self):
        """Fetch data, build statistics, and inject external statistics."""
        end_dt = (datetime.now() - timedelta(days=3)).replace(
            minute=0, second=0, microsecond=0
        )
        start_dt = end_dt - timedelta(days=3)

        try:
            hourly_usage = await self._hass.async_add_executor_job(
                self._fetch_meter_usage, start_dt, end_dt, "H"
            )
        except Exception:
            _LOGGER.exception("Failed to fetch hourly meter usage from Thames Water")
            hourly_usage = None

        # Hourly statistics
        if hourly_usage is not None and len(hourly_usage.Lines) > 0:
            _LOGGER.info("Fetched %d hourly entries", len(hourly_usage.Lines))
            hourly_stats = _generate_hourly_statistics_from_meter_usage(
                start_dt.date(), hourly_usage
            )
            self._state = hourly_usage.Lines[-1].Read
            if hourly_stats:
                self._latest_reading_time = hourly_stats[-1]["start"]
            self._inject_statistics(
                f"{DOMAIN}:thameswater_consumption_hourly",
                "Thames Water Consumption (Hourly)",
                hourly_stats,
            )
        else:
            hourly_stats = None
            _LOGGER.warning(
                "Thames Water returned no hourly data for %s to %s",
                start_dt,
                end_dt,
            )

        if hourly_stats is not None:
            self._inject_statistics(
                CONSUMPTION_STATISTIC_ID,
                "Thames Water Consumption",
                hourly_stats,
            )
            await self._async_write_cost_statistics(hourly_stats)

    async def _async_write_cost_statistics(
        self, consumption: list[StatisticData]
    ) -> None:
        """Write what the new readings cost, for the Energy dashboard.

        The dashboard's own cost sensor prices whatever the meter reports at the
        rate showing when it reports it, which cannot put last year's rate on
        last year's water, and Thames Water reports days late.
        """
        tariff = self._tariff_coordinator.data
        if tariff is None:
            _LOGGER.debug("No tariff available yet, so nothing was priced")
            return

        priced_through, running_total = await self._async_cost_watermark()
        if priced_through is None:
            # Nothing has ever been priced, and the readings already recorded
            # are worth as much as the ones just fetched.
            consumption = await self._async_all_consumption(consumption)

        rows = price_readings(consumption, tariff, priced_through, running_total)
        if not rows:
            return
        _LOGGER.debug(
            "Injecting %d cost statistics (first: %s, last: %s)",
            len(rows),
            rows[0]["start"],
            rows[-1]["start"],
        )
        async_add_external_statistics(
            self._hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name="Thames Water Consumption Cost",
                source=DOMAIN,
                statistic_id=COST_STATISTIC_ID,
                unit_class=None,
                unit_of_measurement="GBP",
            ),
            rows,
        )

    async def _async_cost_watermark(self) -> tuple[datetime | None, float]:
        """Return how far cost has been priced, and the total to carry on from."""
        last = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, COST_STATISTIC_ID, True, {"sum"}
        )
        rows = last.get(COST_STATISTIC_ID)
        if not rows:
            return None, 0.0
        return dt_util.utc_from_timestamp(rows[0]["start"]), rows[0].get("sum") or 0.0

    async def _async_all_consumption(
        self, fetched: list[StatisticData]
    ) -> list[StatisticData]:
        """Return every consumption reading on record, the fetched rows winning.

        The rows just fetched are queued for the recorder rather than in it, so
        they are merged over what a read returns.
        """
        history = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            dt_util.utc_from_timestamp(0),
            None,
            {CONSUMPTION_STATISTIC_ID},
            "hour",
            None,
            {"state", "sum"},
        )
        readings = {
            dt_util.utc_from_timestamp(row["start"]): StatisticData(
                start=dt_util.utc_from_timestamp(row["start"]),
                state=row["state"],
                sum=row["sum"],
            )
            for row in history.get(CONSUMPTION_STATISTIC_ID, [])
        }
        readings.update({row["start"]: row for row in fetched})
        return [readings[start] for start in sorted(readings)]


class ThamesWaterBalanceSensor(SensorEntity):
    """Sensor exposing the outstanding balance on the Thames Water account."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_should_poll = False
    _attr_name = "Thames Water Outstanding Balance"

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        account_number: str,
    ) -> None:
        """Initialize the balance sensor."""
        self._hass = hass
        self._username = username
        self._password = password
        self._account_number = account_number
        self._attr_unique_id = f"thames_water_balance_{account_number}"
        self._attr_native_value: float | None = None
        self._current_balance: float | None = None
        self._is_in_credit: bool | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the balance sensor."""
        return DeviceInfo(
            identifiers={(DOMAIN, "thames_water")},
            manufacturer="Thames Water",
            model="Thames Water",
            name="Thames Water Meter",
        )

    @property
    def extra_state_attributes(self) -> dict[str, float | bool | None]:
        """Expose the broader balance picture as attributes."""
        return {
            "current_balance": self._current_balance,
            "is_in_credit": self._is_in_credit,
        }

    @callback
    async def async_update_callback(self, ts) -> None:
        """Triggered by the time interval to refresh and write state."""
        await self.async_update()
        self.async_write_ha_state()

    def _fetch_account(self) -> Account:
        """Fetch account details (blocking; run in executor)."""
        thames_water = ThamesWater(
            email=self._username,
            password=self._password,
            account_number=int(self._account_number),
        )
        return thames_water.get_account()

    async def async_update(self) -> None:
        """Fetch account details and update the sensor state."""
        try:
            account = await self._hass.async_add_executor_job(self._fetch_account)
        except Exception:
            _LOGGER.exception("Failed to fetch account details from Thames Water")
            self._attr_native_value = None
            self._current_balance = None
            self._is_in_credit = None
            return

        self._attr_native_value = float(account.paymentDueAmount)
        self._current_balance = float(account.currentBalance)
        self._is_in_credit = account.isInCredit


class ThamesWaterTariffCoordinator(DataUpdateCoordinator[Tariff]):
    """Coordinator that scrapes the current Thames Water tariff once a day."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the tariff coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Thames Water tariff",
            update_interval=TARIFF_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> Tariff:
        """Fetch and parse the tariff (blocking work runs in an executor)."""
        try:
            return await self.hass.async_add_executor_job(get_tariff)
        except TariffError as err:
            raise UpdateFailed(str(err)) from err


@dataclass(frozen=True, kw_only=True)
class ThamesWaterTariffSensorDescription(SensorEntityDescription):
    """Describes a Thames Water tariff sensor."""

    value_fn: Callable[[Tariff], float]


TARIFF_SENSORS: tuple[ThamesWaterTariffSensorDescription, ...] = (
    ThamesWaterTariffSensorDescription(
        key="unit_rate",
        name="Thames Water Unit Rate",
        native_unit_of_measurement="GBP/L",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=6,
        value_fn=lambda tariff: tariff.unit_rate_per_litre,
    ),
    ThamesWaterTariffSensorDescription(
        key="standing_charge",
        name="Thames Water Standing Charge",
        native_unit_of_measurement="GBP/day",
        icon="mdi:cash-clock",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda tariff: tariff.standing_charge_per_day,
    ),
    ThamesWaterTariffSensorDescription(
        key="volumetric_rate",
        name="Thames Water Volumetric Rate",
        native_unit_of_measurement="GBP/m³",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda tariff: tariff.volumetric_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="clean_water_rate",
        name="Thames Water Clean Water Rate",
        native_unit_of_measurement="GBP/m³",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.clean_water_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="wastewater_rate",
        name="Thames Water Wastewater Rate",
        native_unit_of_measurement="GBP/m³",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.wastewater_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="water_fixed_charge",
        name="Thames Water Water Fixed Charge",
        native_unit_of_measurement="GBP/year",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.water_fixed_per_year,
    ),
    ThamesWaterTariffSensorDescription(
        key="wastewater_fixed_charge",
        name="Thames Water Wastewater Fixed Charge",
        native_unit_of_measurement="GBP/year",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.wastewater_fixed_per_year,
    ),
)


def _lapse_date(effective_date: date) -> date:
    """The 1 April on which charges that took effect on that date lapse."""
    return date(effective_date.year + 1, 4, 1)


def _still_in_force(effective_date: date) -> bool:
    """Whether charges that took effect on that date still apply.

    A charging year runs from 1 April to 31 March, so charges lapse on the
    1 April after the one they started on.
    """
    lapses = _lapse_date(effective_date)
    return datetime.now(ZoneInfo("Europe/London")).date() < lapses


class ThamesWaterTariffSensor(
    CoordinatorEntity[ThamesWaterTariffCoordinator], RestoreSensor
):
    """A sensor derived from the scraped Thames Water tariff.

    The figures hold for a charging year, so the last known ones are worth
    more than nothing while the page is unreachable or a restart is in
    progress. They are restored until a scrape succeeds, or until the
    charging year they belong to ends, whichever comes first.
    """

    entity_description: ThamesWaterTariffSensorDescription

    def __init__(
        self,
        coordinator: ThamesWaterTariffCoordinator,
        description: ThamesWaterTariffSensorDescription,
    ) -> None:
        """Initialize the tariff sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_name = description.name
        self._attr_unique_id = f"thames_water_tariff_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "thames_water_tariff")},
            manufacturer="Thames Water",
            model="Tariff",
            name="Thames Water Tariff",
        )
        self._restored_value: float | None = None
        self._restored_effective_date: date | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known figure, unless its charging year has ended."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is None or last_data is None:
            return

        stored = last_state.attributes.get("effective_date")
        if stored is None:
            return

        effective_date = date.fromisoformat(stored)
        if _still_in_force(effective_date):
            self._restored_value = last_data.native_value
            self._restored_effective_date = effective_date

    @property
    def native_value(self) -> float | None:
        """Return the value derived from the current tariff."""
        if self.coordinator.data is None:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose when the figures took effect.

        A restart reads this back to decide whether the value it restores
        is still the rate in force.
        """
        tariff = self.coordinator.data
        effective_date = (
            tariff.effective_date if tariff else self._restored_effective_date
        )
        if effective_date is None:
            return None
        return {"effective_date": effective_date.isoformat()}
