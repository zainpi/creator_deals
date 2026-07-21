"""Regression tests for Keepa average selection and A/B cache isolation."""
import sys
import types
import unittest

sys.modules.setdefault("keepa", types.SimpleNamespace(Keepa=lambda key: None))
from keepa_service import KeepaService


def stats_with(index, value, key="avg90"):
    values = [-1] * 19
    values[index] = value
    return {"asin": "X", "stats": {key: values}}


class FakeAPI:
    def __init__(self, products_by_domain=None, error=None):
        self.products_by_domain = products_by_domain or {}
        self.error = error
        self.calls = []

    def query(self, asins, **kwargs):
        self.calls.append((asins, kwargs))
        if self.error:
            raise self.error
        return self.products_by_domain.get(kwargs["domain"], [])


class TestKeepaAverages(unittest.TestCase):
    def service(self, api):
        service = object.__new__(KeepaService)
        service.api, service.cache, service._seller_cache = api, {}, {}
        return service

    def test_buy_box_is_used_when_new_is_missing(self):
        product = stats_with(18, 39999)
        self.assertEqual(KeepaService._best_avg_from_product(product), (399.99, 90, 18))

    def test_new_fallback_still_works(self):
        product = stats_with(1, 35000, "avg180")
        self.assertEqual(KeepaService._best_avg_from_product(product), (350.0, 180, 1))

    def test_marketplaces_do_not_share_cached_average(self):
        de, gb = stats_with(18, 40000), stats_with(18, 30000)
        api = FakeAPI({"DE": [de], "GB": [gb]})
        service = self.service(api)
        self.assertEqual(service.get_avg_batch(["X"], "DE")["X"], (400.0, 90))
        self.assertEqual(service.get_avg_batch(["X"], "GB")["X"], (300.0, 90))
        self.assertEqual(len(api.calls), 2)

    def test_api_error_is_not_reported_or_cached_as_no_data(self):
        service = self.service(FakeAPI(error=RuntimeError("token failure")))
        with self.assertRaises(RuntimeError):
            service.get_avg_batch(["X"], "DE")
        self.assertEqual(service.cache, {})

    def test_validation_cache_includes_current_price(self):
        product = stats_with(18, 40000)
        service = self.service(FakeAPI({"DE": [product]}))
        first = service.validate_deals_batch([("X", 200)], "DE")["X"]
        second = service.validate_deals_batch([("X", 300)], "DE")["X"]
        self.assertEqual(first["drop_percent"], 50.0)
        self.assertEqual(second["drop_percent"], 25.0)


if __name__ == "__main__":
    unittest.main()
