#!/usr/bin/env python3
"""Append the current charging year to the tariff table when it turns over.

Charges run from 1 April to 31 March, so ``thameswaterapi/tariffs.csv`` goes
out of date once a year. This scrapes the help page the library already knows
how to read and, when it names a later effective date than the last row,
appends that row for a pull request to carry.

Prints what it did and exits 0 either way; exits 1 only if the page cannot be
read or the figures do not fit the table, both of which are worth a failed run.
"""

import csv
import pathlib
import sys

from thameswaterapi import TARIFF_CSV, TARIFFS, Tariff, TariffError, get_tariff

TABLE = pathlib.Path(__file__).resolve().parent.parent / "thameswaterapi" / TARIFF_CSV

# Decimal places each column is published to, so a new row reads like the
# ones above it.
PLACES = {
    "clean_water_rate_per_m3": 4,
    "wastewater_rate_per_m3": 4,
    "water_fixed_per_year": 2,
    "wastewater_fixed_per_year": 2,
}


def cell(tariff: Tariff, column: str) -> str:
    """Render one column of a tariff at the precision it is published to."""
    if column == "charging_year":
        return str(tariff.charging_year)

    value = getattr(tariff, column)
    text = f"{value:.{PLACES[column]}f}"
    if float(text) != value:
        raise ValueError(f"{column} of {value} does not fit {PLACES[column]} places")
    return text


def main() -> int:
    try:
        current = get_tariff()
    except TariffError as err:
        print(f"Could not read the tariff page: {err}", file=sys.stderr)
        return 1

    newest = max(TARIFFS)
    if current.charging_year <= newest:
        print(f"Up to date: {newest} is still the current charging year")
        return 0

    with TABLE.open(encoding="utf-8", newline="") as table:
        header = next(csv.reader(table))

    try:
        row = ",".join(cell(current, column) for column in header)
    except (AttributeError, KeyError, ValueError) as err:
        print(f"Could not render the new charges: {err}", file=sys.stderr)
        return 1

    with TABLE.open("a", encoding="utf-8", newline="\n") as table:
        table.write(f"{row}\n")

    print(f"Added the charging year beginning {current.effective_date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
