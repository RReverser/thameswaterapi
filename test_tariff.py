import datetime
import unittest
from unittest import mock

import thameswaterapi
from thameswaterapi import (
    TARIFFS,
    Tariff,
    TariffError,
    charging_year,
    parse_tariff,
    tariff_for,
)

# A trimmed sample of the rendered metered-customers help page: the
# volumetric-rate sentence (figures wrapped in <strong>) and the fixed-charge
# table (standard charge and the surface-water-drainage rebate charge).
SAMPLE_HTML = (
    "<p>One cubic metre equals 1,000 litres. It costs "
    "<strong>£2.7346</strong> per m3 for clean water and "
    "<strong>£1.4721</strong> per m3 for wastewater as of 1 April 2026.</p>"
    "<table><tr><th>Type</th><th>Fixed charge</th>"
    "<th>Fixed charge with surface water drainage rebate</th></tr>"
    "<tr><td>Water</td><td>£66.87</td><td>Not applicable</td></tr>"
    "<tr><td>Wastewater</td><td>£128.13</td><td>£80.43</td></tr></table>"
)


class TestParseTariff(unittest.TestCase):
    def test_parses_all_figures(self) -> None:
        tariff = parse_tariff(SAMPLE_HTML)
        self.assertEqual(tariff.clean_water_rate_per_m3, 2.7346)
        self.assertEqual(tariff.wastewater_rate_per_m3, 1.4721)
        self.assertEqual(tariff.water_fixed_per_year, 66.87)
        # The standard fixed charge, not the surface-water-drainage rebate one.
        self.assertEqual(tariff.wastewater_fixed_per_year, 128.13)
        self.assertEqual(tariff.effective_date, datetime.date(2026, 4, 1))

    def test_derived_values(self) -> None:
        tariff = parse_tariff(SAMPLE_HTML)
        self.assertEqual(tariff.volumetric_rate_per_m3, 4.2067)
        self.assertAlmostEqual(tariff.unit_rate_per_litre, 0.0042067)
        self.assertAlmostEqual(tariff.standing_charge_per_day, 0.5342, places=4)

    def test_missing_data_raises(self) -> None:
        with self.assertRaises(TariffError):
            parse_tariff("<html>no tariff here</html>")

    def test_missing_effective_date_raises(self) -> None:
        # Pricing a historical reading needs the date, so a reword that drops
        # it has to fail rather than silently disable per-date pricing.
        with self.assertRaises(TariffError):
            parse_tariff(SAMPLE_HTML.replace(" as of 1 April 2026", ""))

    def test_a_changeover_that_is_not_1_april_raises(self) -> None:
        # A Tariff keeps the year alone, so a page naming another date has to
        # fail here rather than lose the day it named.
        with self.assertRaises(TariffError):
            parse_tariff(SAMPLE_HTML.replace("1 April 2026", "1 July 2026"))


class TestTariffDerivations(unittest.TestCase):
    def test_derivations(self) -> None:
        tariff = Tariff(
            clean_water_rate_per_m3=2.0,
            wastewater_rate_per_m3=1.0,
            water_fixed_per_year=100.0,
            wastewater_fixed_per_year=200.0,
            charging_year=2026,
        )
        self.assertEqual(tariff.effective_date, datetime.date(2026, 4, 1))
        self.assertEqual(tariff.expires, datetime.date(2027, 4, 1))
        self.assertEqual(tariff.volumetric_rate_per_m3, 3.0)
        self.assertAlmostEqual(tariff.unit_rate_per_litre, 0.003)
        self.assertAlmostEqual(tariff.standing_charge_per_day, 300.0 / 365, places=4)


class TestTariffTable(unittest.TestCase):
    def setUp(self) -> None:
        # The scrape is remembered until it expires, so one test's stand-in
        # must not answer the next one's lookup.
        thameswaterapi._scraped = None

    def test_every_row_is_keyed_by_its_own_charging_year(self) -> None:
        for year, tariff in TARIFFS.items():
            self.assertEqual(year, tariff.charging_year)

    def test_a_day_takes_the_charging_year_it_falls_in(self) -> None:
        self.assertEqual(
            tariff_for(datetime.date(2024, 4, 1)).clean_water_rate_per_m3, 1.9145
        )
        self.assertEqual(
            tariff_for(datetime.date(2025, 3, 31)).clean_water_rate_per_m3, 1.9145
        )
        self.assertEqual(
            tariff_for(datetime.date(2025, 4, 1)).clean_water_rate_per_m3, 2.4743
        )

    def test_a_covered_day_is_answered_without_a_request(self) -> None:
        with mock.patch("thameswaterapi.get_tariff", side_effect=AssertionError):
            self.assertEqual(
                tariff_for(datetime.date(2024, 6, 1)).clean_water_rate_per_m3, 1.9145
            )

    def test_charges_run_from_1_april_to_31_march(self) -> None:
        self.assertEqual(charging_year(datetime.date(2026, 3, 31)), 2025)
        self.assertEqual(charging_year(datetime.date(2026, 4, 1)), 2026)
        self.assertEqual(charging_year(datetime.date(2026, 12, 31)), 2026)

    def test_the_dates_a_charging_year_spans(self) -> None:
        tariff = TARIFFS[2026]
        self.assertEqual(tariff.effective_date, datetime.date(2026, 4, 1))
        self.assertEqual(tariff.expires, datetime.date(2027, 4, 1))

    def test_a_day_past_the_table_is_scraped(self) -> None:
        # Rates for a year not yet transcribed come off the page, rather than
        # being guessed at by carrying the newest entry forward.
        scraped = Tariff(
            clean_water_rate_per_m3=9.0,
            wastewater_rate_per_m3=8.0,
            water_fixed_per_year=7.0,
            wastewater_fixed_per_year=6.0,
            charging_year=max(TARIFFS) + 1,
        )
        published = scraped.effective_date
        with mock.patch("thameswaterapi.get_tariff", return_value=scraped) as fetch:
            self.assertEqual(tariff_for(published), scraped)
            # A window of readings is one request, not one apiece, and the
            # rest of the charging year is that same request too.
            self.assertEqual(
                tariff_for(published + datetime.timedelta(days=1)), scraped
            )
            self.assertEqual(
                tariff_for(scraped.expires - datetime.timedelta(days=1)), scraped
            )
        self.assertEqual(fetch.call_count, 1)

    def test_the_page_is_read_again_once_the_scrape_expires(self) -> None:
        rates = {"wastewater_rate_per_m3": 8.0, "water_fixed_per_year": 7.0}
        scraped = Tariff(
            clean_water_rate_per_m3=9.0,
            wastewater_fixed_per_year=6.0,
            charging_year=max(TARIFFS) + 1,
            **rates,
        )
        following = Tariff(
            clean_water_rate_per_m3=10.0,
            wastewater_fixed_per_year=6.0,
            charging_year=scraped.charging_year + 1,
            **rates,
        )
        with mock.patch(
            "thameswaterapi.get_tariff", side_effect=[scraped, following]
        ) as fetch:
            self.assertEqual(tariff_for(scraped.effective_date), scraped)
            self.assertEqual(tariff_for(following.effective_date), following)
        self.assertEqual(fetch.call_count, 2)

    def test_a_day_the_page_does_not_cover_either_raises(self) -> None:
        stale = Tariff(
            clean_water_rate_per_m3=9.0,
            wastewater_rate_per_m3=8.0,
            water_fixed_per_year=7.0,
            wastewater_fixed_per_year=6.0,
            charging_year=max(TARIFFS),
        )
        with (
            mock.patch("thameswaterapi.get_tariff", return_value=stale),
            self.assertRaises(TariffError),
        ):
            tariff_for(datetime.date(max(TARIFFS) + 2, 4, 1))

    def test_a_day_before_the_first_entry_raises(self) -> None:
        with (
            mock.patch("thameswaterapi.get_tariff", side_effect=AssertionError),
            self.assertRaises(TariffError),
        ):
            tariff_for(datetime.date(min(TARIFFS), 3, 31))


if __name__ == "__main__":
    unittest.main()
