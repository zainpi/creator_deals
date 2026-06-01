import keepa
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


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
            sellers = self.api.query_seller(seller_id)
            data = sellers.get(seller_id) if isinstance(sellers, dict) else None
            if data is None and isinstance(sellers, list) and sellers:
                data = sellers[0]
            if not data:
                return None
            rating = data.get("feedbackRating")
            if rating is None:
                rating = data.get("rating")
            result = float(rating) if rating is not None else None
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
                sellers = self.api.query_seller(to_query)
            except Exception as e:
                logger.warning(f"[KEEPA] Seller batch error: {e}")
                sellers = {}
            for sid in to_query:
                data = sellers.get(sid) if isinstance(sellers, dict) else None
                rating = None
                if data:
                    rating = data.get("feedbackRating")
                    if rating is None:
                        rating = data.get("rating")
                val = float(rating) if rating is not None else None
                self._seller_cache[sid] = val
                out[sid] = val
        return out
