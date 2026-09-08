"""Config flow for Thames Water integration."""

import logging

import voluptuous as vol

from homeassistant import config_entries

from .const import DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN
from thameswaterapi import AuthenticationError, ThamesWater

_LOGGER = logging.getLogger(__name__)


class ThamesWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Thames Water."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._credentials: dict = {}
        self._client: ThamesWater | None = None
        self._error: str | None = None

    async def async_step_user(self, user_input=None):
        """Handle the initial step: collect credentials."""
        errors = {}
        if user_input is not None:
            self._credentials = user_input
            try:
                self._client = await self.hass.async_add_executor_job(
                    self._authenticate,
                    user_input["username"],
                    user_input["password"],
                )
            except AuthenticationError:
                _LOGGER.exception("Thames Water rejected the sign-in")
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Could not sign in to Thames Water")
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_account()

        data_schema = vol.Schema(
            {
                vol.Required(
                    "username", description={"suggested_value": "email@example.com"}
                ): str,
                vol.Required("password"): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_account(self, user_input=None):
        """Handle the account selection step."""
        assert self._client is not None

        ok, account_numbers = await self._fetch(
            self._client.get_account_numbers, "list the accounts"
        )
        if not ok:
            return self._abort_reason()
        # A login with one account, or none listed at all, has nothing to
        # choose between, and the client already holds the account its own
        # token names as the default.
        choosing = len(account_numbers) > 1
        if choosing:
            if user_input is None:
                return self._show_account_form(account_numbers)
            # The meter endpoints answer for the account whose page was last
            # visited. Assigning marks the session as needing that visit
            # again, and the next data call makes it.
            self._client.account_number = int(user_input["account_number"])

        # `get_account` names the account in a header taken from
        # `account_number`, so it answers for whichever one is set.
        ok, account = await self._fetch(self._client.get_account, "load the account")
        if not ok:
            return self._abort_reason()
        if not account.is_smart_metered:
            if not choosing:
                return self.async_abort(reason="not_smart_metered")
            return self._show_account_form(
                account_numbers, {"base": "not_smart_metered"}
            )

        self._credentials["account_number"] = str(self._client.account_number)
        return await self.async_step_meter()

    def _show_account_form(
        self, account_numbers: list[int], errors: dict[str, str] | None = None
    ):
        """Ask which contract account to read."""
        data_schema = vol.Schema(
            {
                vol.Required("account_number"): vol.In(
                    {str(n): str(n) for n in account_numbers}
                ),
            }
        )
        return self.async_show_form(
            step_id="account", data_schema=data_schema, errors=errors
        )

    async def async_step_meter(self, user_input=None):
        """Handle the meter selection step."""
        assert self._client is not None
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title="Thames Water",
                data={
                    **self._credentials,
                    "meter_id": user_input["meter_id"],
                    "update_interval_hours": user_input["update_interval_hours"],
                },
            )

        ok, meter_numbers = await self._fetch(
            self._client.get_meter_numbers, "list the meters"
        )
        if not ok:
            return self._abort_reason()
        if not meter_numbers:
            errors["base"] = "no_meters"
            return self.async_show_form(
                step_id="meter", data_schema=vol.Schema({}), errors=errors
            )

        data_schema = vol.Schema(
            {
                vol.Required("meter_id"): vol.In(meter_numbers),
                vol.Required(
                    "update_interval_hours",
                    default=DEFAULT_UPDATE_INTERVAL_HOURS,
                ): vol.All(int, vol.Range(min=1)),
            }
        )

        return self.async_show_form(
            step_id="meter", data_schema=data_schema, errors=errors
        )

    async def _fetch(self, call, description: str):
        """Run a blocking client call, reporting whether it succeeded.

        `thameswaterapi` talks over `requests`, so every call belongs in an
        executor. A failure past the credentials form has no form to report
        against, and letting it escape leaves the user with nothing but an
        unexplained error, so the reason is kept for `_abort_reason`.

        Success is reported separately from the result, because an empty list
        is an answer these calls do give.
        """
        try:
            return True, await self.hass.async_add_executor_job(call)
        except AuthenticationError:
            _LOGGER.exception(
                "Thames Water rejected the session trying to %s", description
            )
            self._error = "invalid_auth"
        except Exception:
            _LOGGER.exception("Could not %s from Thames Water", description)
            self._error = "cannot_connect"
        return False, None

    def _abort_reason(self):
        """End the flow with the reason `_fetch` last recorded."""
        assert self._error is not None
        return self.async_abort(reason=self._error)

    @staticmethod
    def _authenticate(username: str, password: str) -> ThamesWater:
        """Authenticate with Thames Water (blocking, run in executor)."""
        client = ThamesWater(email=username, password=password)
        client.authenticate()
        return client
