# Thames Water Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

# Home Assistant Integration for Thames Water Consumption Data

This Home Assistant integration retrieves water consumption data from Thames Water using their API. It allows you to monitor your water usage directly from your Home Assistant setup without needing additional devices.

You need a Thames Water Smart Meter. The water consumption data provided by this integration is delayed by approximately three days or more. This delay is a characteristic of the Thames Water data system and cannot be altered in this integration.

It uses the [thameswaterapi](https://github.com/jelmer/thameswaterapi) Python package to interact with the Thames Water API.

## Sensors

The integration exposes the following entities:

* **Water consumption** — the latest hourly meter read in litres, with hourly external statistics injected for use in the Energy dashboard, together with a matching **cost** statistic in GBP (see [Water cost in the Energy dashboard](#water-cost-in-the-energy-dashboard)).
* **Outstanding balance** — the amount currently due on your Thames Water account, in GBP. The current balance and an `is_in_credit` flag are exposed as attributes.
* **Tariff** — the current metered-household charges for the Thames Water region:
  * **Unit Rate** (`GBP/L`) — combined clean water + wastewater volumetric rate, per litre.
  * **Standing Charge** (`GBP/day`) — combined water + wastewater fixed charge per day.
  * **Volumetric Rate** (`GBP/m³`) — the combined rate per cubic metre.
  * Individual **Clean Water Rate**, **Wastewater Rate**, **Water Fixed Charge** and **Wastewater Fixed Charge** sensors (disabled by default; enable them for a full bill breakdown).

  Thames Water has no tariff API — metered charges are a fixed annual "Scheme of Charges", published per region rather than per account, so the same figures apply to every customer. This integration reads them from Thames Water's public [metered customers](https://www.thameswater.co.uk/help/account-and-billing/understand-your-bill/metered-customers) help page (no credentials required) and refreshes daily; they normally only change on 1 April. The fixed charges use the standard rate (not the surface-water-drainage rebate rate). If the page changes shape the sensors keep reporting the figures last read (until the next charging year), and consumption and balance are unaffected.

### Water cost in the Energy dashboard

The integration writes a cost statistic, **thames_water:thameswater_consumption_cost**, pricing each hourly reading at the unit rate of its charging year. In **Settings → Dashboards → Energy → Water consumption**, edit the source, choose *Use an entity tracking the total costs* and select that statistic.

Readings are priced once, when they first arrive, so later tariff changes do not rewrite past costs. Readings are left unpriced until a tariff covering them has been read. The standing charge is not included.

## Installation

### Installation through HACS

1. Install the custom component using the Home Assistant Community Store (HACS) by adding the Custom Repository:
https://github.com/jelmer/HA-Thames-Water
2. In the HACS panel, select Thames Water from the repository list and select the DOWNLOAD button.
3. Restart HA
4. Go to Settings > Devices & Services > Add Integration and select Thames Water.

### Manual installation

Copy the `custom_components/thames_water/` directory and all of its files to your `config/custom_components/` directory.

## Configuration

Once installed, restart Home Assistant:

[![Open your Home Assistant instance and show the system dashboard.](https://my.home-assistant.io/badges/system_dashboard.svg)](https://my.home-assistant.io/redirect/system_dashboard/)

Then, add the integration:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=thames_water)


<details>
  <summary>Manually add the Integration</summary>
  Visit the <i>Integrations</i> section in Home Assistant and click the <i>Add</i> button in the bottom right corner. Search for <code>Thames Water</code> and input your credentials. <b>You may need to clear your browser cache before the integration appears in the list.</b>
</details>

## Energy Management

The water statistics can be integrated into HA [Home Energy Management](https://www.home-assistant.io/docs/energy/) using **thames_water:thameswater_consumption**.

It will attempt to fetch the latest data at 00:00 and 12:00 every day.

[![Open your Home Assistant instance and show your Energy configuration panel.](https://my.home-assistant.io/badges/config_energy.svg)](https://my.home-assistant.io/redirect/config_energy/)

![Dashboard](./dashboard.png)

## Acknowledgements

This integration is based on the original work by [Ayrton Bourn (AyrtonB)](https://github.com/AyrtonB).
