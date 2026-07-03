import unittest

from thameswaterapi import Tariff, TariffError, parse_tariff

# A trimmed sample of the rendered metered-customers help page: the
# volumetric-rate sentence (figures wrapped in <strong>) and the fixed-charge
# table (standard charge and the surface-water-drainage rebate charge).
SAMPLE_HTML = (
    "<p>One cubic metre equals 1,000 litres. It costs "
    "<strong>£2.7346</strong> per m3 for clean water and "
    "<strong>£1.4721</strong> per m3 for wastewater.</p>"
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

    def test_derived_values(self) -> None:
        tariff = parse_tariff(SAMPLE_HTML)
        self.assertEqual(tariff.volumetric_rate_per_m3, 4.2067)
        self.assertAlmostEqual(tariff.unit_rate_per_litre, 0.0042067)
        self.assertAlmostEqual(tariff.standing_charge_per_day, 0.5342, places=4)

    def test_missing_data_raises(self) -> None:
        with self.assertRaises(TariffError):
            parse_tariff("<html>no tariff here</html>")


class TestTariffDerivations(unittest.TestCase):
    def test_derivations(self) -> None:
        tariff = Tariff(
            clean_water_rate_per_m3=2.0,
            wastewater_rate_per_m3=1.0,
            water_fixed_per_year=100.0,
            wastewater_fixed_per_year=200.0,
        )
        self.assertEqual(tariff.volumetric_rate_per_m3, 3.0)
        self.assertAlmostEqual(tariff.unit_rate_per_litre, 0.003)
        self.assertAlmostEqual(tariff.standing_charge_per_day, 300.0 / 365, places=4)


if __name__ == "__main__":
    unittest.main()
