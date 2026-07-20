"""
Unit tests for deal_scoring.py — the deterministic scoring that replaced the
AI's hallucinated math. These lock down the formulas so results stay consistent
with the AI's estimated price ranges.

Run:  python3 -m unittest test_deal_scoring -v
"""

import unittest

from deal_scoring import compute_scores, to_product_fields, PRODUCT_FIELDS

# A clean, strong deal used as a baseline across tests.
GOOD = {"product": "Sony WH-1000XM4", "retail_low": 90, "retail_high": 110,
        "resale_low": 80, "resale_high": 100}


class TestCoreMath(unittest.TestCase):
    def test_profit_is_resale_midpoint_minus_buy_no_fees(self):
        r = compute_scores(GOOD, 40)
        # midpoint resale = (80+100)/2 = 90 ; profit = 90 - 40 = 50 (no fees)
        self.assertEqual(r["resale_mid"], 90.0)
        self.assertEqual(r["estimated_profit"], 50.0)

    def test_roi_and_margin(self):
        r = compute_scores(GOOD, 40)
        self.assertAlmostEqual(r["roi_pct"], 125.0, places=1)      # 50/40
        self.assertAlmostEqual(r["margin_pct"], 55.56, places=1)   # 50/90

    def test_discount_is_vs_retail_midpoint(self):
        r = compute_scores(GOOD, 40)
        # retail mid = 100 ; discount = (100-40)/100 = 60%
        self.assertEqual(r["discount_pct"], 60.0)

    def test_midpoint_is_used(self):
        # Asymmetric ranges: midpoints, not ends, must drive the numbers.
        r = compute_scores({"retail_low": 100, "retail_high": 200,
                            "resale_low": 60, "resale_high": 140}, 50)
        self.assertEqual(r["retail_mid"], 150.0)
        self.assertEqual(r["resale_mid"], 100.0)
        self.assertEqual(r["estimated_profit"], 50.0)


class TestScores(unittest.TestCase):
    def test_buying_score_is_discount_depth(self):
        # target_discount_pct default 50 -> 50% below retail == full 100
        r = compute_scores({"retail_low": 100, "retail_high": 100,
                            "resale_low": 100, "resale_high": 100}, 50)
        self.assertEqual(r["discount_pct"], 50.0)
        self.assertEqual(r["buying_score"], 100.0)

    def test_buying_score_scales_and_clamps(self):
        r = compute_scores({"retail_low": 100, "retail_high": 100,
                            "resale_low": 100, "resale_high": 100}, 75)  # 25% off
        self.assertEqual(r["buying_score"], 50.0)  # 25/50*100

    def test_resell_score_reflects_profit_and_roi(self):
        r = compute_scores(GOOD, 40)  # profit 50 (=target), roi 125% (>target)
        self.assertEqual(r["resell_score"], 100.0)

    def test_scores_never_exceed_bounds(self):
        r = compute_scores({"retail_low": 500, "retail_high": 500,
                            "resale_low": 500, "resale_high": 500}, 1)
        for k in ("buying_score", "resell_score", "overall_score"):
            self.assertGreaterEqual(r[k], 0.0)
            self.assertLessEqual(r[k], 100.0)

    def test_missing_retail_leaves_buying_neutral(self):
        # No retail range -> can't measure discount -> buying falls back to neutral
        r = compute_scores({"resale_low": 80, "resale_high": 100}, 40)
        self.assertIsNone(r["discount_pct"])
        self.assertEqual(r["buying_score"], 50.0)   # neutral_score default
        self.assertEqual(r["estimated_profit"], 50.0)


class TestPenalties(unittest.TestCase):
    def test_weak_discount_penalised(self):
        r = compute_scores({"retail_low": 100, "retail_high": 100,
                            "resale_low": 130, "resale_high": 130}, 95)  # only 5% off
        types = [p["type"] for p in r["penalties"]]
        self.assertIn("weak_discount", types)

    def test_wide_range_penalised(self):
        # resale spread 120% of mid (> 0.60 threshold)
        r = compute_scores({"retail_low": 100, "retail_high": 100,
                            "resale_low": 40, "resale_high": 160}, 40)
        types = [p["type"] for p in r["penalties"]]
        self.assertIn("wide_range", types)
        self.assertEqual(r["range_uncertainty"], 1.2)

    def test_thin_margin_penalised(self):
        r = compute_scores({"retail_low": 60, "retail_high": 60,
                            "resale_low": 52, "resale_high": 52}, 48)  # profit 4 (<10)
        types = [p["type"] for p in r["penalties"]]
        self.assertIn("thin_margin", types)

    def test_negative_margin_heavily_penalised(self):
        r = compute_scores({"retail_low": 130, "retail_high": 150,
                            "resale_low": 90, "resale_high": 110}, 120)  # profit -20
        types = [p["type"] for p in r["penalties"]]
        self.assertIn("negative_margin", types)
        self.assertEqual(r["overall_score"], 0.0)

    def test_clean_deal_has_no_penalties(self):
        r = compute_scores(GOOD, 40)
        self.assertEqual(r["penalties"], [])
        self.assertEqual(r["overall_score"], 100.0)


class TestFallbacks(unittest.TestCase):
    def test_no_estimate_is_neutral_not_crash(self):
        r = compute_scores({}, 40)
        self.assertFalse(r["estimate_ok"])
        self.assertEqual(r["overall_score"], 50.0)
        self.assertIn("No usable", r["ai_reason"])

    def test_zero_or_missing_buy_price_is_neutral(self):
        self.assertEqual(compute_scores(GOOD, 0)["overall_score"], 50.0)
        self.assertEqual(compute_scores(GOOD, None)["overall_score"], 50.0)

    def test_garbage_estimate_values_ignored(self):
        r = compute_scores({"resale_low": "n/a", "resale_high": None}, 40)
        self.assertFalse(r["estimate_ok"])   # nothing usable to price against


class TestConfigOverrides(unittest.TestCase):
    def test_resale_point_low_changes_math(self):
        cfg = {"scoring": {"resale_point": "low"}}
        r = compute_scores(GOOD, 40, cfg)
        self.assertEqual(r["resale_mid"], 80.0)          # low end, not midpoint
        self.assertEqual(r["estimated_profit"], 40.0)

    def test_target_discount_override_changes_buying(self):
        cfg = {"scoring": {"target_discount_pct": 100}}
        r = compute_scores({"retail_low": 100, "retail_high": 100,
                            "resale_low": 100, "resale_high": 100}, 40, cfg)  # 60% off
        self.assertEqual(r["buying_score"], 60.0)        # 60/100*100

    def test_penalty_can_be_disabled(self):
        cfg = {"scoring": {"negative_margin_penalty": 0}}
        r = compute_scores({"retail_low": 130, "retail_high": 150,
                            "resale_low": 90, "resale_high": 110}, 120, cfg)
        types = [p["type"] for p in r["penalties"]]
        self.assertNotIn("negative_margin", types)


class TestProductMapping(unittest.TestCase):
    def test_ai_score_mirrors_overall(self):
        r = compute_scores(GOOD, 40)
        fields = to_product_fields(r)
        self.assertEqual(fields["ai_score"], r["overall_score"])
        self.assertTrue(fields["ai_reason"])

    def test_all_product_fields_present(self):
        fields = to_product_fields(compute_scores(GOOD, 40))
        for k in PRODUCT_FIELDS:
            self.assertIn(k, fields)

    def test_determinism(self):
        a = compute_scores(GOOD, 40)
        b = compute_scores(GOOD, 40)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
