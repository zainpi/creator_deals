import keepa
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Keepa CSV/stats price-type index. KeepaBot-master reads stats["avg"][1]
# (see country_query_configs.py -> "priceTypes": [1]); index 1 = NEW price.
KEEPA_PRICE_TYPE_NEW = 1


class KeepaService:
    """
    Keepa validation service for deal discovery.
    
    Can reuse Keepa parsing logic from dealsbrowser.py
    and productTrackerV2.py for production.
    """
    
    def __init__(self, config_or_key):
        # Support both config dict and plain API key
        if isinstance(config_or_key, dict):
            api_key = config_or_key.get("keepa", {}).get("api_key")
        else:
            api_key = config_or_key
        
        self.api = keepa.Keepa(api_key)
        self.cache = {}
        self._seller_cache = {}  # seller_id -> feedback rating %

    def _build_validation(self, product, current_price):
        """Turn a raw Keepa product + price into our validation dict (or None)."""
        stats = product.get("stats", {}) or {}
        avg90 = stats.get("avg90_BUY_BOX_SHIPPING")
        if avg90 is None:
            return None
        # Convert from Keepa cents to euros
        avg90 = avg90 / 100
        drop_percent = round(((avg90 - current_price) / avg90) * 100, 2) \
            if (avg90 > 0 and current_price) else 0
        return {
            "avg90": avg90,
            "drop_percent": drop_percent,
            "sales_rank": product.get("salesRankReference"),
            "monthly_sold": stats.get("salesRankDrops90"),
            "asin": product.get("asin"),
            "validated_at": str(datetime.now()),
        }

    def validate_deal(self, asin, current_price, domain="DE"):
        """
        Validate a single deal using Keepa stats.

        Returns dict with:
        - avg90: 90-day average price
        - drop_percent: current drop % from 90-day avg
        - sales_rank: current sales rank
        - monthly_sold: estimated monthly sold count
        """

        try:
            # Check cache first
            if asin in self.cache:
                return self.cache[asin]

            products = self.api.query(asin, domain=domain, stats=90, history=False)
            if not products:
                logger.warning(f"[KEEPA] No product data for {asin}")
                return None

            result = self._build_validation(products[0], current_price)
            if result is None:
                logger.warning(f"[KEEPA] No 90-day stats for {asin}")
                return None
            result["asin"] = asin

            self.cache[asin] = result
            logger.info(f"[KEEPA] Validated {asin}: {result['drop_percent']}% drop from €{result['avg90']:.2f}")
            return result

        except Exception as e:
            logger.error(f"[KEEPA] Validation error for {asin}: {e}")
            return None

    def validate_deals_batch(self, items, domain="DE"):
        """
        Validate many deals in a single Keepa request (up to 100 ASINs per call).

        `items` is an iterable of (asin, current_price) tuples.
        Returns {asin: result_dict_or_None}. Cached ASINs are served from cache;
        only the un-cached ones are fetched, and missing ASINs are cached as None.
        """
        results = {}
        price_map = {}
        to_query = []
        for asin, price in items:
            if not asin:
                continue
            price_map[asin] = price
            if asin in self.cache:
                results[asin] = self.cache[asin]
            elif asin not in to_query:
                to_query.append(asin)

        for i in range(0, len(to_query), 100):
            chunk = to_query[i:i + 100]
            try:
                products = self.api.query(chunk, domain=domain, stats=90, history=False) or []
            except Exception as e:
                logger.error(f"[KEEPA] Batch query error: {e}")
                products = []

            returned = set()
            for product in products:
                asin = product.get("asin")
                if not asin:
                    continue
                returned.add(asin)
                result = self._build_validation(product, price_map.get(asin))
                if result is not None:
                    result["asin"] = asin
                self.cache[asin] = result
                results[asin] = result

            # Cache misses as None so we don't re-query them this run
            for asin in chunk:
                if asin not in returned:
                    self.cache[asin] = None
                    results[asin] = None

        if to_query:
            logger.info(f"[KEEPA] Batch-validated {len(to_query)} ASIN(s) in {((len(to_query) - 1) // 100) + 1} request(s)")
        return results

    # =====================================================================
    # 90-day average price — same method as KeepaBot-master
    # =====================================================================

    @staticmethod
    def _avg90_from_product(product, price_type=KEEPA_PRICE_TYPE_NEW):
        """
        Read the 90-day average price from a Keepa product exactly like
        KeepaBot-master's dealsBrowser does:

            stats = product["stats"]              # requires stats=90 on query
            avg   = stats["avg"][price_type]      # Keepa cents, price_type 1 = NEW
            price = avg / 100                     # -> currency units

        Keepa uses -1 (and sometimes 0) to mean "no data". Returns a float
        price in currency units, or None when unavailable.
        """
        stats = product.get("stats") or {}
        avg_arr = stats.get("avg")
        if isinstance(avg_arr, list) and len(avg_arr) > price_type:
            val = avg_arr[price_type]
            if isinstance(val, (int, float)) and val > 0 and val != -1:
                return round(val / 100, 2)
        return None

    def get_avg90_batch(self, asins, domain="DE", price_type=KEEPA_PRICE_TYPE_NEW):
        """
        Fetch the Keepa 90-day average price for many ASINs (<=100 per request).

        Returns {asin: avg90_price_or_None}. Uses/fills the shared self.cache
        keyed as ("avg90", asin, price_type) so repeated runs are cheap.
        """
        out = {}
        to_query = []
        for asin in asins:
            if not asin:
                continue
            ck = ("avg90", asin, price_type)
            if ck in self.cache:
                out[asin] = self.cache[ck]
            elif asin not in to_query:
                to_query.append(asin)

        for i in range(0, len(to_query), 100):
            chunk = to_query[i:i + 100]
            try:
                products = self.api.query(chunk, domain=domain, stats=90, history=False) or []
            except Exception as e:
                logger.error(f"[KEEPA] avg90 batch error: {e}")
                products = []

            returned = set()
            for product in products:
                asin = product.get("asin")
                if not asin:
                    continue
                returned.add(asin)
                val = self._avg90_from_product(product, price_type)
                self.cache[("avg90", asin, price_type)] = val
                out[asin] = val

            # Cache misses as None so we don't re-query them this run.
            for asin in chunk:
                if asin not in returned:
                    self.cache[("avg90", asin, price_type)] = None
                    out[asin] = None

        if to_query:
            logger.info(f"[KEEPA] avg90 for {len(to_query)} ASIN(s) in "
                        f"{((len(to_query) - 1) // 100) + 1} request(s)")
        return out

    def get_avg90_by_eans(self, eans, domain="DE", price_type=KEEPA_PRICE_TYPE_NEW):
        """
        Look up the Keepa 90-day average price by EAN/GTIN code instead of ASIN.

        Some ASINs the Creators API returns (seller/marketplace variants) are
        tracked thinly or not at all on a given Keepa domain, while the same
        product resolves fine via its EAN. Keepa's query supports code lookups
        (product_code_is_asin=False).

        Returns {ean: avg90_price_or_None}. Cached under ("avg90ean", ean).
        """
        out = {}
        to_query = []
        for ean in eans:
            if not ean:
                continue
            ean = str(ean)
            ck = ("avg90ean", ean, price_type)
            if ck in self.cache:
                out[ean] = self.cache[ck]
            elif ean not in to_query:
                to_query.append(ean)

        for i in range(0, len(to_query), 100):
            chunk = to_query[i:i + 100]
            try:
                products = self.api.query(
                    chunk, domain=domain, stats=90, history=False,
                    product_code_is_asin=False,
                ) or []
            except Exception as e:
                logger.error(f"[KEEPA] avg90-by-EAN batch error: {e}")
                products = []

            # Map every EAN a returned product advertises to its avg90, so we can
            # match back to the code we queried with.
            matched = {}
            for product in products:
                avg = self._avg90_from_product(product, price_type)
                codes = []
                for key in ("eanList", "upcList"):
                    vals = product.get(key) or []
                    codes.extend(str(v) for v in vals if v)
                for code in codes:
                    if code not in matched or matched[code] is None:
                        matched[code] = avg

            for ean in chunk:
                val = matched.get(ean)
                self.cache[("avg90ean", ean, price_type)] = val
                out[ean] = val

        if to_query:
            logger.info(f"[KEEPA] avg90-by-EAN for {len(to_query)} code(s)")
        return out

    @staticmethod
    def _coerce_rating(data):
        """Extract a numeric positive-feedback % from a Keepa seller record.
        Keepa may return the rating as a scalar or as a history array, so handle both."""
        if not isinstance(data, dict):
            return None
        val = data.get("currentRating")
        if val is None:
            val = data.get("feedbackRating")
        if val is None:
            val = data.get("rating")
        if isinstance(val, (list, tuple)):
            nums = [x for x in val if isinstance(x, (int, float))]
            val = nums[-1] if nums else None
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def get_seller_rating(self, seller_id: str, domain: str = "DE"):
        """
        Return seller positive feedback % (0-100) from Keepa.
        Returns None if unavailable.
        """
        if not seller_id:
            return None
        if seller_id in self._seller_cache:
            return self._seller_cache[seller_id]
        try:
            sellers = self.api.seller_query(seller_id, domain=domain)
            data = sellers.get(seller_id) if isinstance(sellers, dict) else None
            if data is None and isinstance(sellers, list) and sellers:
                data = sellers[0]
            result = self._coerce_rating(data)
            self._seller_cache[seller_id] = result
            return result
        except Exception as e:
            logger.warning(f"[KEEPA] Seller rating error for {seller_id}: {e}")
            return None

    def get_seller_ratings_batch(self, seller_ids, domain="DE"):
        """
        Fetch positive-feedback % for many sellers in one Keepa request.
        Returns {seller_id: rating_or_None}. Uses/fills the per-seller cache.
        """
        out = {}
        to_query = []
        for sid in seller_ids:
            if not sid:
                continue
            if sid in self._seller_cache:
                out[sid] = self._seller_cache[sid]
            elif sid not in to_query:
                to_query.append(sid)

        if to_query:
            try:
                sellers = self.api.seller_query(to_query, domain=domain)
            except Exception as e:
                logger.warning(f"[KEEPA] Seller batch error: {e}")
                sellers = {}
            for sid in to_query:
                data = sellers.get(sid) if isinstance(sellers, dict) else None
                val = self._coerce_rating(data)
                self._seller_cache[sid] = val
                out[sid] = val
        return out
