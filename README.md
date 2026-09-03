# thameswaterapi

`pip install thameswaterapi`

A Python client for retrieving meter data from Thames Water.

This is a fork of [thameswaterclient](https://github.com/AyrtonB/Thames-Water/)
by [Ayrton Bourn](https://github.com/AyrtonB).

## Python API

```python
from thameswaterapi import ThamesWater

email = "myname@provider.com"
password = "**********"

thames_water = ThamesWater(email=email, password=password)
```

If you have multiple accounts, you can specify which one to use:

```python
thames_water = ThamesWater(email=email, password=password, account_number=123456789)
```

### Listing accounts and meters

```python
thames_water.get_account_numbers()  # [123456789011, 123456789012]
thames_water.get_meter_numbers()  # ['123456789']
```

### Daily usage

```python
from thameswaterapi import lines_to_timeseries

meters = thames_water.get_meters()
readings = lines_to_timeseries(meters.Lines)
for r in readings:
    print(r.start, r.usage, r.total)
```

### Hourly usage

```python
import datetime
from thameswaterapi import meter_usage_lines_to_timeseries

meter = thames_water.get_meter_numbers()[0]
start = datetime.datetime(2024, 10, 1)
end = datetime.datetime(2024, 10, 2)

meter_usage = thames_water.get_meter_usage(meter, start, end)
readings = meter_usage_lines_to_timeseries(start, meter_usage.Lines)
for r in readings:
    print(r.hour_start, r.usage, r.total)
```

### Tariff

Thames Water has no tariff API — metered charges are a fixed annual "Scheme of
Charges" published per region (identical for every customer), so the figures are
scraped from Thames Water's public metered-customers help page and need no
authentication:

```python
from thameswaterapi import get_tariff

tariff = get_tariff()
tariff.clean_water_rate_per_m3  # 2.7346
tariff.wastewater_rate_per_m3  # 1.4721
tariff.water_fixed_per_year  # 66.87
tariff.wastewater_fixed_per_year  # 128.13  (standard rate, not the rebate)

tariff.volumetric_rate_per_m3  # combined GBP/m3
tariff.unit_rate_per_litre  # combined GBP/L
tariff.standing_charge_per_day  # combined fixed charge GBP/day
```

`ThamesWater.get_tariff()` is also available on an authenticated client (it
reuses the session).

## Command line

```
python -m thameswaterapi EMAIL PASSWORD [options]
```

Options:

- `--account-number N` — use a specific contract account number (defaults to the account default)
- `--list-accounts` — list available contract account numbers and exit
- `--list-meters` — list meter serial numbers and exit
- `--meter M` — query a specific meter (defaults to first meter)

Examples:

```sh
# Show daily and hourly readings for the default account and first meter
python -m thameswaterapi myname@provider.com mypassword

# List available account numbers
python -m thameswaterapi myname@provider.com mypassword --list-accounts

# List meters on a specific account
python -m thameswaterapi myname@provider.com mypassword --account-number 123456789012 --list-meters

# Query a specific meter
python -m thameswaterapi myname@provider.com mypassword --meter 123456789
```
