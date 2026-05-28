import asyncio
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
    """Main deal discovery and processing scheduler."""
    
    def __init__(self, config, stats):
        self.config = config
        self.stats = stats

        self.searcher = CreatorsSearch(config)
        # Load blocked categories from config
        from filters import set_blocked_categories
        set_blocked_categories(config)
        
        self.keepa = None
        self.keepa = None
        try:
            keepa_key = config.get("keepa", {}).get("api_key", "")
            keepa_enabled = config.get("keepa", {}).get("enabled", True)
            if keepa_enabled and keepa_key:
                self.keepa = KeepaService(keepa_key)
        except Exception:
            self.keepa = None

        # Initialize AI scorer with full config (handles disabled/missing keys internally)
        self.ai = AIScorer(self.config)
        
        self.discord = DiscordAlerts(
            config.get("discord", {}).get("webhook_url", "")
        )

        self.running = False
        self.min_ai_score = config.get("ai", {}).get("minimum_score", 7)
        self.basic_mode = bool(config.get("creators", {}).get("basic_mode", False))

    async def scan_loop(self):
        """Main scanning loop."""
        self.running = True
        logger.info("[SCHEDULER] Deal scanner loop started")

        while self.running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                logger.info("[SCHEDULER] Scan loop cancelled")
                break
            except Exception as e:
                logger.error(f"[SCHEDULER] Scanner error: {e}")

            await asyncio.sleep(
                self.config.get("scanner", {}).get("scan_interval_seconds", 15)
            )

    async def scan_once(self):
        """Perform one complete scan."""
        self.stats.scan_count += 1
        self.stats.last_scan_time = datetime.now().isoformat()
        
        logger.info(f"[SCHEDULER] Starting scan #{self.stats.scan_count}")
        
        pages_to_scan = int(self.config.get("scanner", {}).get("pages_to_search", 1))

        # Determine marketplaces to scan: config.scanner.marketplaces, else derive from Amazon_* keys, else default list
        marketplaces = []
        try:
            marketplaces = list(self.config.get("scanner", {}).get("marketplaces", []))
        except Exception:
            marketplaces = []
        if not marketplaces:
            try:
                marketplaces = [k.split("_")[1] for k in self.config.keys() if k.startswith("Amazon_")]
            except Exception:
                marketplaces = []
        if not marketplaces:
            marketplaces = ["DE", "GB", "FR", "ES", "IT"]

        for mk in marketplaces:
            try:
                # Switch searcher marketplace and reload credentials
                self.searcher.marketplace = mk
                try:
                    self.searcher._load_credentials()
                except Exception:
                    pass

                logger.info(f"[SCHEDULER] Scanning marketplace {mk} for {pages_to_scan} page(s)")
                for page in range(1, pages_to_scan + 1):
                    try:
                        logger.debug(f"[SCHEDULER] {mk} page {page}")
                        items = self.searcher.search_items(page=page)

                        for item in items:
                            if self.running:
                                # Attach marketplace context for downstream logic
                                item = dict(item)
                                item["_marketplace"] = mk
                                item["_page"] = page
                                await self.process_item(item)
                    except Exception as e:
                        logger.error(f"[SCHEDULER] {mk} page {page} error: {e}")
            except Exception as e:
                logger.error(f"[SCHEDULER] Marketplace {mk} error: {e}")

        self.stats.log_summary()

    async def process_item(self, item):
        """Process a single discovered item."""
        # Preserve marketplace/page metadata across normalization
        ctx_marketplace = item.get("_marketplace")
        ctx_page = item.get("_page")

        # Normalize structure to PascalCase for filters regardless of API shape
        try:
            norm = self.searcher._normalize_item(item)
            if isinstance(norm, dict):
                item = norm
        except Exception:
            pass

        # Restore metadata if present
        if ctx_marketplace:
            item["_marketplace"] = ctx_marketplace
        if ctx_page is not None:
            item["_page"] = ctx_page

        asin = item.get("ASIN")

        if not asin:
            return

        # Skip if already discovered
        if asin_exists(asin):
            return

        self.stats.total_found += 1

        # Filter by category (skip in basic mode)
        category = DealFilters.extract_category(item)
        if not self.basic_mode and not DealFilters.category_allowed(category):
            self.stats.category_filtered += 1
            return

        # Extract listing info
        listings = item.get("OffersV2", {}).get("Listings", [])
        listing = listings[0] if listings else {}

        # Extract and validate price (allow missing in basic mode)
        price = DealFilters.extract_price(listing) if listing else None
        if not self.basic_mode and not price:
            return

        # Filter by seller (skip in basic mode to show results)
        seller_data = DealFilters.extract_seller(listing)
        if not self.basic_mode and not DealFilters.seller_allowed(seller_data.get("seller_name")):
            self.stats.seller_filtered += 1
            return

        # Validate with Keepa (optional / configurable)
        keepa_data = None
        try:
            keepa_enabled = self.config.get("keepa", {}).get("enabled", True)
            if keepa_enabled and self.keepa and price is not None:
                mk_ctx = item.get("_marketplace") or self.config.get("amazon", {}).get("marketplace", "DE")
                # Keepa expects 'GB', not 'UK'
                keepa_domain = "GB" if str(mk_ctx).upper() in ("GB", "UK") else str(mk_ctx).upper()
                keepa_data = self.keepa.validate_deal(
                    asin,
                    price,
                    domain=keepa_domain,
                )
            else:
                keepa_data = None
        except Exception as e:
            logger.warning(f"[SCHEDULER] Keepa error for {asin}: {e}")
            keepa_data = None

        if keepa_data:
            self.stats.keepa_passed += 1
        else:
            if self.config.get("keepa", {}).get("enabled", True):
                self.stats.keepa_failed += 1

        # Build product record
        mk_out = item.get("_marketplace") or self.config.get("amazon", {}).get("marketplace", "DE")
        savings_val = DealFilters.extract_savings_percent(listing)
        product = {
            "asin": asin,
            "title": DealFilters.extract_title(item),
            "marketplace": mk_out,
            "current_price": price,
            "savings_percent": savings_val,
            "category": category,
            "seller_name": seller_data.get("seller_name"),
            "seller_id": seller_data.get("seller_id"),
            "keepa_avg_90": (keepa_data or {}).get("avg90"),
            "keepa_drop_percent": (keepa_data or {}).get("drop_percent"),
            "keepa_sales_rank": (keepa_data or {}).get("sales_rank"),
            "keepa_monthly_sold": (keepa_data or {}).get("monthly_sold"),
            "image": DealFilters.extract_image(item)
        }

        # Attach discovery page number if available
        page_found = item.get("_page")
        if page_found is not None:
            product["page_found"] = int(page_found)

        # Optionally enforce server-side filters BEFORE AI
        try:
            if (self.config.get("filters", {}) or {}).get("apply_to_new", False):
                min_save = int((self.config.get("amazon", {}) or {}).get("min_saving_percent", 0) or 0)
                max_price_cfg = float((self.config.get("amazon", {}) or {}).get("max_price", 0) or 0)
                min_keepa = int((self.config.get("filters", {}) or {}).get("min_keepa_drop_percent", 0) or 0)
                keepa_enabled = self.config.get("keepa", {}).get("enabled", True)

                # Savings threshold
                if savings_val is None or (min_save > 0 and (int(savings_val) if isinstance(savings_val, (int, float)) else 0) < min_save):
                    return

                # Price threshold
                if max_price_cfg > 0 and (price is None or float(price) > max_price_cfg):
                    return

                # Keepa drop threshold (only enforced when keepa enabled and min > 0)
                if keepa_enabled and min_keepa > 0:
                    drop = (keepa_data or {}).get("drop_percent")
                    if drop is None or float(drop) < float(min_keepa):
                        return
        except Exception:
            pass

        # Score with AI if enabled
        if self.config.get("ai", {}).get("enabled", True) and self.ai:
            try:
                title = product.get("title") or ""
                # Only score when we have a title; prefer after Keepa validation
                if title:
                    score = self.ai.score_deal(title, asin)
                    product["ai_score"] = float(score)
                    product["ai_reason"] = ""

                    if product["ai_score"] >= self.min_ai_score:
                        self.stats.ai_passed += 1
                        # Send to Discord
                        if self.discord.send(product):
                            self.stats.discord_posted += 1
                            product["posted"] = True
                    else:
                        self.stats.ai_failed += 1
                        # If enforcing filters server-side, drop items below AI threshold
                        if (self.config.get("filters", {}) or {}).get("apply_to_new", False):
                            return
                else:
                    product["ai_score"] = 0
                    product["ai_reason"] = "Missing title"
            except Exception as e:
                logger.error(f"[SCHEDULER] AI scoring error for {asin}: {e}")
                self.stats.ai_failed += 1
        else:
            # No AI scoring
            product["ai_score"] = 0
            product["ai_reason"] = "AI disabled"

        # Save to database (deduped by PRIMARY KEY asin)
        try:
            insert_product(product)
        except Exception as e:
            logger.error(f"[SCHEDULER] Database insert error for {asin}: {e}")

    def stop(self):
        """Stop the scanner."""
        self.running = False
        logger.info("[SCHEDULER] Stop requested")
