"""Tests for the Thames Water config flow."""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from thameswaterapi import AuthenticationError

from custom_components.thames_water.const import DOMAIN

CREDENTIALS = {"username": "someone@example.com", "password": "hunter2"}


@pytest.fixture
def mock_recorder_before_hass() -> Generator[None]:
    """Stand in for the recorder the manifest depends on.

    The integration lists `recorder` so the sensor platform can write
    statistics. Nothing in the config flow reaches it, and setting a real one
    up needs a database prepared before `hass` exists, which the autouse
    fixture enabling custom integrations rules out.
    """
    with patch(
        "homeassistant.components.recorder.async_setup",
        return_value=True,
    ):
        yield


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return result["flow_id"]


class _Client:
    """A signed-in client whose account lookup fails the way the site does."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.account_number = 12345678

    def get_account_numbers(self) -> list[int]:
        raise self._error


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AuthenticationError("session rejected"), "invalid_auth"),
        (OSError("connection reset"), "cannot_connect"),
    ],
)
async def test_a_failure_listing_accounts_aborts_with_a_reason(
    hass: HomeAssistant,
    error: Exception,
    reason: str,
) -> None:
    """A failure after sign-in names its cause rather than escaping the flow."""
    flow_id = await _start(hass)
    with patch(
        "custom_components.thames_water.config_flow."
        "ThamesWaterConfigFlow._authenticate",
        return_value=_Client(error),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, CREDENTIALS)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == reason


async def test_a_failure_to_sign_in_is_logged(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`cannot_connect` tells the user to read the log, so write to it."""
    flow_id = await _start(hass)
    with patch(
        "custom_components.thames_water.config_flow."
        "ThamesWaterConfigFlow._authenticate",
        side_effect=OSError("connection reset"),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, CREDENTIALS)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert "connection reset" in caplog.text


class _AccountClient:
    """A client that signs in and lists an account, then fails on the detail."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.account_number = 12345678

    def get_account_numbers(self) -> list[int]:
        return [self.account_number]

    def get_account(self) -> object:
        raise self._error


async def test_a_failure_loading_the_account_aborts_with_a_reason(
    hass: HomeAssistant,
) -> None:
    """The account lookup after the listing is guarded too."""
    flow_id = await _start(hass)
    with patch(
        "custom_components.thames_water.config_flow."
        "ThamesWaterConfigFlow._authenticate",
        return_value=_AccountClient(OSError("connection reset")),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, CREDENTIALS)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


class _MeterClient(_AccountClient):
    """A client that reaches the meter step before failing."""

    def get_account(self) -> object:
        return SimpleNamespace(is_smart_metered=True)

    def get_meter_numbers(self) -> list[str]:
        raise self._error


async def test_a_failure_listing_meters_aborts_with_a_reason(
    hass: HomeAssistant,
) -> None:
    """The meter listing is the last blocking call the flow makes."""
    flow_id = await _start(hass)
    with patch(
        "custom_components.thames_water.config_flow."
        "ThamesWaterConfigFlow._authenticate",
        return_value=_MeterClient(AuthenticationError("session expired")),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, CREDENTIALS)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


class _NoMeterClient(_AccountClient):
    """A smart-metered account that lists no meters."""

    def get_account(self) -> object:
        return SimpleNamespace(is_smart_metered=True)

    def get_meter_numbers(self) -> list[str]:
        return []


async def test_an_empty_meter_list_is_an_answer_not_a_failure(
    hass: HomeAssistant,
) -> None:
    """No meters is a reported error on the form, not an abort."""
    flow_id = await _start(hass)
    with patch(
        "custom_components.thames_water.config_flow."
        "ThamesWaterConfigFlow._authenticate",
        return_value=_NoMeterClient(OSError("unused")),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, CREDENTIALS)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "meter"
    assert result["errors"] == {"base": "no_meters"}
