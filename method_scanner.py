"""Method 1 vs Method 2 comparison engine.

Implements the "Overall flow" from the Creators API monitor board as a
standalone engine, separate from continuous_scanner.py's learned-baseline
scanner (the two run side by side — see worker.py).

Method 1 = search using a category's SUBCATEGORY browse nodes.
Method 2 = search using just the top-level category's PARENT browse node.
Both are browse-node-only searches (no keywords, no searchIndex filter) so the
comparison isolates the one variable that differs between them: node
granularity. Results are posted to separate Discord channels
(discord.method_webhooks.method1 / method2) so the two can be judged head to
head.

Per node (one row in the `method_nodes` table), one tick does:
  1. Creators API search across the node's current €min_price -> max_price
     window, up to 100 ASINs (10 pages x 10 items/page via
     CreatorsSearch.diagnostic_search, which already paginates + dedupes).
  2. Advance that node's price cursor to the highest price seen (rounded up),
     wrapping back to €0 once the ceiling is reached or a window is empty.
  3. Price-aware ASIN cache check: skip anything already cached at the same
     price, BEFORE spending a Keepa call on it.
  4. Keepa validation: reject unless the price is >= keepa_drop_percent (25%
     by default) below the Keepa 90-day average.
  5. AI scoring 0-100: reject under ai.minimum_score (50 by default).
  6. Post survivors to the method-specific Discord channel.
Then the round robin advances to the next enabled node.
"""

import json
import logging
import math
import os
import time
from datetime import datetime

from creators_search import CreatorsSearch
from keepa_service import KeepaService
from ai_scoring import AIScorer
from discord_alerts import DiscordAlerts
from filters import DealFilters
from database import (
    seed_method_node,
    get_method_nodes,
    update_method_node_price,
    get_method_asin_cache_batch,
    upsert_method_asin_cache_batch,
    bump_method_stats,
    get_method_stats,
    get_method_engine_state,
    update_method_engine_state,
    insert_product,
    METHOD_FEED_USER_IDS,
)

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_CATEGORIES_PATH = os.path.join(_DATA_DIR, "categories.json")
_TOPCATEGORIES_PATH = os.path.join(_DATA_DIR, "topcategories.json")

# Categories enabled by default when nodes are first seeded — matches the
# board's worked example (Appliances / Electronics, both methods). Everything
# else is seeded disabled; flip on via the "Pick a Category" UI once it has
# browse-node data (see /api/method_test/config).
_DEFAULT_ENABLED_CATEGORIES = {"Appliances", "Electronics"}


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[METHOD] Could not load {path}: {e}")
        return []


class MethodScanner:
    """One scan tick = one price-window batch for one (category, method) node."""

    def __init__(self, config):
        self.config = config
        mt = config.get("method_test", {}) or {}

        self.marketplace = str(mt.get("marketplace", "DE")).upper()
        self.max_price = float(mt.get("max_price", 450))
        self.keepa_drop_percent = float(mt.get("keepa_drop_percent", 25))
        self.daily_budget_requests = int(mt.get("daily_budget_requests", 8640))
        self.tick_seconds = max(1, int(mt.get("tick_seconds", 20)))

        self.searcher = CreatorsSearch(config)

        self.keepa = None
        keepa_cfg = config.get("keepa", {}) or {}
        if keepa_cfg.get("enabled") and keepa_cfg.get("api_key"):
            try:
                self.keepa = KeepaService(keepa_cfg.get("api_key"))
            except Exception as e:
                logger.error(f"[METHOD] Keepa init failed: {e}")
        if not self.keepa:
            logger.warning("[METHOD] Keepa is OFF — nodes will still sweep price "
                           "ranges (for stats) but nothing can pass the 25% gate "
                           "without Keepa configured.")

        self.ai = AIScorer(config)
        self.min_ai_score = float(config.get("ai", {}).get("minimum_score", 50))

        discord_cfg = config.get("discord", {}) or {}
        self.discord = DiscordAlerts(discord_cfg.get("webhook_url", ""))
        method_webhooks = discord_cfg.get("method_webhooks", {}) or {}
        self.webhook_by_method = {
            1: method_webhooks.get("method1", ""),
            2: method_webhooks.get("method2", ""),
        }

        self._seed_nodes()

    # ------------------------------------------------------------------ seeding

    def _seed_nodes(self):
        """Populate method_nodes from data/categories.json (Method 1, one row
        per unique subcategory browse node) and data/topcategories.json
        (Method 2, one row per category that has a parentBrowseNodeId).
        Idempotent — safe to call on every startup."""
        cats = _load_json(_CATEGORIES_PATH)
        tops = _load_json(_TOPCATEGORIES_PATH)

        # Method 1: dedupe by (searchIndex, browseNodeId) — several curated
        # keyword rows often share one subcategory node. Search browse-node-only
        # (no keyword) so Method 1 and Method 2 are an apples-to-apples test of
        # node granularity, not keyword coverage.
        seen_nodes = {}
        for row in cats:
            idx = row.get("searchIndex")
            node = row.get("browseNodeId")
            if not idx or not node:
                continue
            key = (idx, node)
            if key not in seen_nodes:
                seen_nodes[key] = row.get("keywords") or node

        top_by_index = {t.get("searchIndex"): t for t in tops if t.get("searchIndex")}

        for (idx, node), label in seen_nodes.items():
            display = (top_by_index.get(idx) or {}).get("displayName") or idx
            enabled = 1 if idx in _DEFAULT_ENABLED_CATEGORIES else 0
            seed_method_node(
                self.marketplace, idx, 1, node,
                label=f"{display} → {label}", keywords=None, enabled=enabled,
            )

        # Method 2: one node per category with a known parent browse node id.
        for idx, t in top_by_index.items():
            parent = t.get("parentBrowseNodeId")
            if not parent:
                continue
            enabled = 1 if idx in _DEFAULT_ENABLED_CATEGORIES else 0
            seed_method_node(
                self.marketplace, idx, 2, str(parent),
                label=t.get("displayName") or idx, keywords=None, enabled=enabled,
            )

    # --------------------------------------------------------------- scan tick

    def run_once(self):
        """Execute one tick against the next enabled node in the round robin.
        Returns the node processed (for logging), or None if idle/paused."""
        state = get_method_engine_state()
        now = datetime.utcnow().isoformat()

        if not state.get("enabled"):
            update_method_engine_state(running=0, last_heartbeat=now)
            return None

        if self._budget_exhausted():
            update_method_engine_state(
                running=0, last_heartbeat=now,
                current_target=f"Daily budget reached ({self.daily_budget_requests} calls) — resumes tomorrow",
            )
            return None

        nodes = get_method_nodes(marketplace=self.marketplace, enabled_only=True)
        if not nodes:
            update_method_engine_state(running=0, last_heartbeat=now,
                                       current_target="No categories enabled")
            return None

        pointer = int(state.get("rr_pointer", 0) or 0) % len(nodes)
        node = nodes[pointer]
        human = f'{node["marketplace"]} · {node["category"]} · Method {node["method"]} · €{node.get("current_min_price", 0):.0f}+'
        update_method_engine_state(
            running=1, rr_pointer=(pointer + 1) % len(nodes),
            last_heartbeat=now, current_target=human, last_error=None,
        )
        logger.info(f"[METHOD] tick: {human}")

        try:
            self._process_node(node)
        except Exception as e:
            logger.exception(f"[METHOD] node processing error: {e}")
            update_method_engine_state(last_error=str(e)[:200], last_heartbeat=datetime.utcnow().isoformat())
        return node

    def _budget_exhausted(self):
        today_calls = sum(r.get("creators_api_calls") or 0 for r in get_method_stats())
        return today_calls >= self.daily_budget_requests

    # ------------------------------------------------------------- one node

    def _process_node(self, node):
        marketplace = node["marketplace"]
        category = node["category"]
        method = int(node["method"])
        browse_node_id = node["browse_node_id"]
        min_price = float(node.get("current_min_price") or 0)

        self.searcher.marketplace = marketplace
        try:
            self.searcher._load_credentials()
        except Exception:
            pass

        result = self.searcher.diagnostic_search(
            search_index="All",
            keywords="",
            browse_node_id=browse_node_id,
            sort_by="Price:LowToHigh",
            min_saving_percent=0,
            min_price=min_price,
            max_price=self.max_price,
            item_count=10,
            max_pages=10,
            use_keepa=False,  # this engine applies its own flat Keepa gate below
            delivery_flags=["FulfilledByAmazon"],  # board note: "FBA: ON"
        )
        pages_scanned = int((result.get("response") or {}).get("pages_scanned") or 0)
        items = result.get("items") or []
        if pages_scanned:
            bump_method_stats(marketplace, category, method, creators_api_calls=pages_scanned)

        self._advance_price_cursor(marketplace, category, method, browse_node_id, min_price, items)

        if not items:
            return

        survivors = self._cache_and_keepa_gate(marketplace, category, method, items)
        if not survivors:
            return

        self._score_and_post(marketplace, category, method, survivors)

    def _advance_price_cursor(self, marketplace, category, method, browse_node_id, min_price, items):
        """Bump the price floor to the highest price seen (rounded up to the next
        euro), wrapping back to 0 once the ceiling is reached or a window comes
        back empty (nothing left to discover there right now)."""
        prices = []
        for it in items:
            p = self._price_of(it)
            if p is not None:
                prices.append(p)

        if prices:
            next_min = math.ceil(max(prices))
        else:
            next_min = None

        if next_min is None or next_min <= min_price or next_min >= self.max_price:
            new_cursor = 0.0
        else:
            new_cursor = float(next_min)

        update_method_node_price(marketplace, category, method, browse_node_id, new_cursor)

    def _cache_and_keepa_gate(self, marketplace, category, method, items):
        # ---- price-aware ASIN cache check, BEFORE spending a Keepa call ----
        by_asin = {}
        for it in items:
            asin = it.get("ASIN")
            price = self._price_of(it)
            if asin and price is not None:
                by_asin[asin] = (it, price)

        cached = get_method_asin_cache_batch(marketplace, method, list(by_asin.keys()))
        to_process = []
        skipped = 0
        for asin, (it, price) in by_asin.items():
            cached_price = cached.get(asin)
            if cached_price is not None and abs(float(cached_price) - float(price)) < 0.01:
                skipped += 1
                continue
            to_process.append((it, asin, price))

        if skipped:
            bump_method_stats(marketplace, category, method, cache_skipped=skipped)
        if not to_process:
            return []

        upsert_method_asin_cache_batch(marketplace, method, [(a, p) for _, a, p in to_process])
        bump_method_stats(marketplace, category, method, asins_scanned=len(to_process))

        # ---- Keepa: reject unless >= keepa_drop_percent below the 90-day avg ----
        if not self.keepa:
            return []

        asins = [a for _, a, _ in to_process]
        try:
            avg90_map = self.keepa.get_avg90_batch(asins, domain=marketplace) or {}
            bump_method_stats(marketplace, category, method,
                              keepa_calls=max(1, (len(asins) + 99) // 100))
        except Exception as e:
            logger.error(f"[METHOD] Keepa batch error: {e}")
            return []

        survivors, rejected = [], 0
        for it, asin, price in to_process:
            avg90 = avg90_map.get(asin)
            if not avg90 or avg90 <= 0:
                rejected += 1
                continue
            drop = (float(avg90) - price) / float(avg90) * 100
            if drop < self.keepa_drop_percent:
                rejected += 1
                continue
            it["_keepa_avg90"] = round(float(avg90), 2)
            it["_keepa_drop"] = round(drop, 1)
            survivors.append((it, asin, price))

        if rejected:
            bump_method_stats(marketplace, category, method, keepa_rejected=rejected)
        return survivors

    def _score_and_post(self, marketplace, category, method, survivors):
        webhook_url = self.webhook_by_method.get(method, "")
        feed_user_id = METHOD_FEED_USER_IDS.get(method)

        ai_rejected = 0
        posted = 0
        for it, asin, price in survivors:
            title = DealFilters.extract_title(it) or ""
            score = 50.0
            try:
                if self.ai.enabled and title:
                    score = float(self.ai.score_deal(title, asin))
            except Exception as e:
                logger.warning(f"[METHOD] AI error for {asin}: {e}")

            if score < self.min_ai_score:
                ai_rejected += 1
                continue

            product = self._build_product(it, asin, price, category, marketplace, method, score)

            sent = False
            try:
                sent = self.discord.send(product, webhook_url=webhook_url)
            except Exception as e:
                logger.warning(f"[METHOD] Discord error for {asin}: {e}")
            product["posted"] = sent
            if sent:
                posted += 1

            if feed_user_id:
                try:
                    insert_product({**product, "user_id": feed_user_id})
                except Exception as e:
                    logger.error(f"[METHOD] DB insert error for {asin}: {e}")

        if ai_rejected:
            bump_method_stats(marketplace, category, method, ai_rejected=ai_rejected)
        if posted:
            bump_method_stats(marketplace, category, method, posted=posted)

    @staticmethod
    def _build_product(item, asin, price, category, marketplace, method, ai_score):
        listing = MethodScanner._first_listing(item)
        seller = DealFilters.extract_seller(listing)
        return {
            "asin": asin,
            "title": DealFilters.extract_title(item) or "",
            "marketplace": marketplace,
            "current_price": price,
            "savings_percent": item.get("_keepa_drop"),
            "category": f"{category} (Method {method})",
            "seller_name": seller.get("seller_name") or "Unknown",
            "seller_id": seller.get("seller_id"),
            "keepa_avg_90": item.get("_keepa_avg90"),
            "keepa_drop_percent": item.get("_keepa_drop"),
            "ai_score": ai_score,
            "ai_reason": "",
            "image": DealFilters.extract_image(item),
        }

    @staticmethod
    def _first_listing(item):
        listings = (item.get("OffersV2", {}) or {}).get("Listings", []) or []
        return listings[0] if listings else {}

    @staticmethod
    def _price_of(item):
        listing = MethodScanner._first_listing(item)
        return DealFilters.extract_price(listing) if listing else None


class MethodScanLoop:
    """Drives MethodScanner.run_once() forever, sleeping between ticks."""

    def __init__(self, config):
        self.config = config
        self.scanner = MethodScanner(config)
        self.interval = self.scanner.tick_seconds

    def run_forever(self):
        logger.info(f"[METHOD] Starting method-comparison loop (interval {self.interval}s, "
                    f"marketplace {self.scanner.marketplace})")
        while True:
            try:
                self.scanner.run_once()
            except Exception as e:
                logger.exception(f"[METHOD] tick failed: {e}")
                try:
                    update_method_engine_state(last_error=str(e)[:200],
                                               last_heartbeat=datetime.utcnow().isoformat())
                except Exception:
                    pass
            time.sleep(self.interval)
