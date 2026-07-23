import unittest

from filters import DealFilters


class TestCurrencyDropGate(unittest.TestCase):
    def setUp(self):
        self.min_drop_currency = 30.0
        self.average_floor = 60.0

    def test_below_60_skips_absolute_drop_gate(self):
        average = 59.99
        currency_drop = 20.0
        rejected = DealFilters.absolute_drop_gate_applies(average, self.average_floor) and currency_drop < self.min_drop_currency
        self.assertFalse(rejected)

    def test_60_or_more_still_requires_30_drop(self):
        average = 60.0
        currency_drop = 20.0
        rejected = DealFilters.absolute_drop_gate_applies(average, self.average_floor) and currency_drop < self.min_drop_currency
        self.assertTrue(rejected)

    def test_60_or_more_accepts_30_drop(self):
        average = 80.0
        currency_drop = 30.0
        rejected = DealFilters.absolute_drop_gate_applies(average, self.average_floor) and currency_drop < self.min_drop_currency
        self.assertFalse(rejected)

    def test_five_credentials_recompute_total_pool(self):
        daily_budget, tick_seconds = DealFilters.scan_limits(5, 8640, 10)
        self.assertEqual(43200, daily_budget)
        self.assertEqual(20, tick_seconds)

    def test_continuous_scanner_reserve_recomputes_safe_method_tick(self):
        total_budget, _ = DealFilters.scan_limits(5, 8640, 10)
        continuous_reserve = 86400 // 15
        method_budget = total_budget - continuous_reserve
        tick_seconds = DealFilters.scan_limits(1, method_budget, 10)[1]
        self.assertEqual(5760, continuous_reserve)
        self.assertEqual(37440, method_budget)
        self.assertEqual(24, tick_seconds)


if __name__ == "__main__":
    unittest.main()
