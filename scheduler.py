import logging
from datetime import datetime

from creators_search import CreatorsSearch
from keepa_service import KeepaService
from ai_scoring import AIScorer
from discord_alerts import DiscordAlerts
from filters import DealFilters
from database import asin_exists, insert_product

logger = logging.getLogger(__name__)


class DealScheduler:
    """Per-user on-demand deal search and processing."""

    def __init__(self, config, stats):
        self.config = config
        self.stats = stats

        self.searcher = CreatorsSearch(config)
        from filters import set_blocked_categories
        set_blocked_categories(config)

        self.keepa = None
        try:
            keepa_key = config.get("keepa", {}).get("api_key", "")
            keepa_enabled = config.get("keepa", {}).get("enabled", True)
            if keepa_enabled and keepa_key:
                self.keepa = KeepaService(keepa_key)
        except Exception:
            self.keepa = None

        self.ai = AIScorer(self.config)

        self.discord = DiscordAlerts(
            config.get("discord", {}).get("webhook_url", "")
        )

        self.min_ai_score = config.get("ai", {}).get("minimum_score", 7)
        self.basic_mode = bool(config.get("creators", {}).get("basic_mode", False))

    def search_for_user(self, user_id: str, keywords: str, marketplaces: list,
                        pages: int, min_saving: int, max_price: float,
                        filters: dict = None, sort_by: str = None) -> list:
        """
        Run an on-demand Creators API search for one user.
        Stores results in DB tagged with user_id and returns them.
        filters dict keys: use_filters, min_saving, min_ai_score,
                           min_seller_rating, min_price, max_price
        sort_by: Creators API sortBy value (e.g. "Price:LowToHigh", "Featured").
        """
        self.stats.scan_count += 1
        self.stats.last_scan_time = datetime.now().isoformat()
        saved = []

        for mk in marketplaces:
            try:
                self.searcher.marketplace = mk
                try:
                    self.searcher._load_credentials()
                except Exception:
                    pass

                for page in range(1, pages + 1):
                    try:
                        search_kwargs = dict(
                            page=page,
                            keywords=keywords or None,
                            min_saving_percent=min_saving,
                            max_price=float(max_price),
                        )
                        if sort_by:
                            search_kwargs["sort_by"] = sort_by
                        items = self.searcher.search_items(**search_kwargs)
                        for item in items:
                            item = dict(item)
                            item["_marketplace"] = mk
                            item["_page"] = page
                            product = self._process_item(user_id, item, filters=filters)
                            if product:
                                saved.append(product)
                    except Exception as e:
                        logger.error(f"[SCHEDULER] {mk} page {page} error: {e}")
            except Exception as e:
                logger.error(f"[SCHEDULER] Marketplace {mk} error: {e}")

        self.stats.api_calls = self.searcher.api_calls
        self.stats.log_summary()
        return saved

    def _process_item(self, user_id: str, item: dict, filters: dict = None):
        """Process a single item for a specific user. Returns product dict or None."""
        ctx_marketplace = item.get("_marketplace")
        ctx_page = item.get("_page")

        try:
            norm = self.searcher._normalize_item(item)
            if isinstance(norm, dict):
                item = norm
        except Exception:
            pass

        if ctx_marketplace:
            item["_marketplace"] = ctx_marketplace
        if ctx_page is not None:
            item["_page"] = ctx_page

        asin = item.get("ASIN")
        if not asin:
            return None

        if asin_exists(user_id, asin):
            return None

        self.stats.total_found += 1

        category = DealFilters.extract_category(item)
        if not self.basic_mode and not DealFilters.category_allowed(category):
            self.stats.category_filtered += 1
            return None

        listings = item.get("OffersV2", {}).get("Listings", [])
        listing = listings[0] if listings else {}

        price = DealFilters.extract_price(listing) if listing else None
        if not self.basic_mode and not price:
            return None

        seller_data = DealFilters.extract_seller(listing)
        if not self.basic_mode and not DealFilters.seller_allowed(seller_data.get("seller_name")):
            self.stats.seller_filtered += 1
            return None

        mk_ctx = item.get("_marketplace") or self.config.get("amazon", {}).get("marketplace", "DE")
        keepa_domain = "GB" if str(mk_ctx).upper() in ("GB", "UK") else str(mk_ctx).upper()
        keepa_enabled = self.config.get("keepa", {}).get("enabled", True)
        # Keepa only actually runs when enabled AND a client was created (api key present)
        keepa_active = bool(keepa_enabled and self.keepa)

        keepa_data = None
        try:
            if keepa_active and price is not None:
                keepa_data = self.keepa.validate_deal(asin, price, domain=keepa_domain)
        except Exception as e:
            logger.warning(f"[SCHEDULER] Keepa error for {asin}: {e}")

        if keepa_data:
            self.stats.keepa_passed += 1
        elif keepa_enabled:
            self.stats.keepa_failed += 1

        # Seller rating only comes from Keepa. When Keepa is off, default to 0
        # so the field is always numeric (never None) and a "min rating ≥ 0"
        # filter still lets everything through.
        seller_rating = None if keepa_active else 0
        try:
            if keepa_active and seller_data.get("seller_id"):
                seller_rating = self.keepa.get_seller_rating(
                    seller_data["seller_id"], domain=keepa_domain
                )
        except Exception as e:
            logger.warning(f"[SCHEDULER] Seller rating error: {e}")

        mk_out = item.get("_marketplace") or self.config.get("amazon", {}).get("marketplace", "DE")
        savings_val = DealFilters.extract_savings_percent(listing)

        product = {
            "user_id": user_id,
            "asin": asin,
            "title": DealFilters.extract_title(item),
            "marketplace": mk_out,
            "current_price": price,
            "savings_percent": savings_val,
            "category": category,
            "seller_name": seller_data.get("seller_name"),
            "seller_id": seller_data.get("seller_id"),
            "seller_rating": seller_rating,
            "keepa_avg_90": (keepa_data or {}).get("avg90"),
            "keepa_drop_percent": (keepa_data or {}).get("drop_percent"),
            "keepa_sales_rank": (keepa_data or {}).get("sales_rank"),
            "keepa_monthly_sold": (keepa_data or {}).get("monthly_sold"),
            "image": DealFilters.extract_image(item),
        }

        page_found = item.get("_page")
        if page_found is not None:
            product["page_found"] = int(page_found)

        # AI scoring
        if self.config.get("ai", {}).get("enabled", True) and self.ai:
            try:
                title = product.get("title") or ""
                if title:
                    score = self.ai.score_deal(title, asin)
                    product["ai_score"] = float(score)
                    product["ai_reason"] = ""
                    if product["ai_score"] >= self.min_ai_score:
                        self.stats.ai_passed += 1
                        if self.discord.send(product):
                            self.stats.discord_posted += 1
                            product["posted"] = True
                    else:
                        self.stats.ai_failed += 1
                else:
                    product["ai_score"] = 0
                    product["ai_reason"] = "Missing title"
            except Exception as e:
                logger.error(f"[SCHEDULER] AI scoring error for {asin}: {e}")
                self.stats.ai_failed += 1
        else:
            product["ai_score"] = 0
            product["ai_reason"] = "AI disabled"

        # Apply server-side filters before saving
        if filters and filters.get("use_filters", True):
            f_min_saving       = filters.get("min_saving", 0)
            f_min_ai           = filters.get("min_ai_score", 0)
            f_min_seller       = filters.get("min_seller_rating", 0)
            f_min_price        = filters.get("min_price", 0)
            f_max_price        = filters.get("max_price", 0)

            sv = product.get("savings_percent") or 0
            if f_min_saving > 0 and sv < f_min_saving:
                return None

            ai = product.get("ai_score") or 0
            if f_min_ai > 0 and ai < f_min_ai:
                return None

            sr = product.get("seller_rating")
            if f_min_seller > 0 and (sr is None or sr < f_min_seller):
                return None

            cp = product.get("current_price") or 0
            if f_min_price > 0 and cp < f_min_price:
                return None
            if f_max_price > 0 and cp > f_max_price:
                return None

        try:
            insert_product(product)
        except Exception as e:
            logger.error(f"[SCHEDULER] DB insert error for {asin}: {e}")

        return product

    def stop(self):
        pass
