"""Category browse-node scanning engine.

Implements the "Overall flow" from the Creators API monitor board as a
standalone engine, separate from continuous_scanner.py's learned-baseline
scanner (the two run side by side — see worker.py).

Searches each category's curated subcategory browse nodes without keywords.
Results are posted to the configured scanner Discord channels
(discord.method_webhooks.method1), tiered by Keepa drop % (90d,
preferring longer windows — 180d/365d — over the 30d last-resort fallback)
(tier90 / tier70 / tier50 / rest), with Keepa+AI rejects going to each
method's trash channel, so the two can be judged head to head.

Per node (one row in the `method_nodes` table), one tick does:
  1. Creators API search across the node's current €min_price -> max_price
     window, up to 100 ASINs (10 pages x 10 items/page via
     CreatorsSearch.diagnostic_search, which already paginates + dedupes).
  2. Advance that node's price cursor to the highest price seen (rounded up),
     wrapping back to €0 once the ceiling is reached or a window is empty.
  3. Price-aware ASIN cache check: skip anything already cached at the same
     price, BEFORE spending a Keepa call on it.
  4. Keepa validation: reject unless the price is both >= keepa_drop_percent
     below the best available Keepa average (90d, preferring longer windows
     180d/365d when 90d has no data, 30d only as a last resort), and at
     least min_drop_currency below that average.
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
from deal_scoring import compute_scores, to_product_fields
from discord_alerts import DiscordAlerts
from filters import DealFilters
from database import (
    seed_method_node,
    delete_stale_method_nodes,
    delete_method_nodes_outside_marketplaces,
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

        self.marketplaces = ["DE"]
        self.marketplace = "DE"
        raw_credential_sources = mt.get("credential_marketplaces") or ["DE"]
        self.credential_marketplaces = [
            str(m).upper() for m in raw_credential_sources
        ] or ["DE"]
        self.max_price = float(mt.get("max_price", 450))
        self.keepa_drop_percent = float(mt.get("keepa_drop_percent", 30))
        self.min_drop_currency = float(mt.get("min_drop_currency", 30))
        self.min_drop_currency_avg_floor = float(
            mt.get("min_drop_currency_avg_floor", 60)
        )
        self.daily_budget_per_credential = int(
            mt.get("daily_budget_per_credential", 8640)
        )
        self.max_pages_per_tick = max(1, int(mt.get("max_pages_per_tick", 10)))
        computed_daily_budget, computed_tick_seconds = DealFilters.scan_limits(
            len(self.credential_marketplaces),
            self.daily_budget_per_credential,
            self.max_pages_per_tick,
        )
        self.daily_budget_requests = int(
            mt.get("daily_budget_requests", computed_daily_budget)
        )
        # Recompute if a manual total-budget override is supplied.
        if self.daily_budget_requests != computed_daily_budget:
            computed_tick_seconds = math.ceil(
                86400 * self.max_pages_per_tick
                / max(1, self.daily_budget_requests)
            )
        self.tick_seconds = max(
            1, int(mt.get("tick_seconds", computed_tick_seconds))
        )

        # One searcher per credential source preserves an independent OAuth
        # token cache and rate-limit window for every pool. All target Germany.
        self.searchers = {}
        for source in self.credential_marketplaces:
            searcher = CreatorsSearch(config)
            searcher.marketplace = "DE"
            searcher._load_credentials(
                source, allow_generic=(source == "DE")
            )
            self.searchers[source] = searcher
        self.searcher = self.searchers[self.credential_marketplaces[0]]

        self.keepa = None
        keepa_cfg = config.get("keepa", {}) or {}
        if keepa_cfg.get("enabled") and keepa_cfg.get("api_key"):
            try:
                self.keepa = KeepaService(keepa_cfg.get("api_key"))
            except Exception as e:
                logger.error(f"[METHOD] Keepa init failed: {e}")
        if not self.keepa:
            logger.warning("[METHOD] Keepa is OFF — nodes will still sweep price "
                           f"ranges (for stats) but nothing can pass the "
                           f"{self.keepa_drop_percent:.0f}% / €{self.min_drop_currency:.2f} "
                           "gates without Keepa configured.")

        self.ai = AIScorer(config)
        self.min_ai_score = float(config.get("ai", {}).get("minimum_score", 50))

        discord_cfg = config.get("discord", {}) or {}
        self.discord = DiscordAlerts(discord_cfg.get("webhook_url", ""))
        method_webhooks = discord_cfg.get("method_webhooks", {}) or {}
        self.webhook_by_method = {
            1: self._normalize_webhooks(method_webhooks.get("method1")),
        }

        self._seed_nodes()

    @staticmethod
    def _normalize_webhooks(entry):
        """Return {tier90, tier70, tier50, rest, trash} -> url. Accepts either
        the tiered dict from config.yml or a legacy single-URL string (which
        then feeds every tier except trash)."""
        tiers = {"tier90": "", "tier70": "", "tier50": "", "rest": "", "trash": ""}
        if isinstance(entry, dict):
            for k in tiers:
                url = str(entry.get(k) or "")
                tiers[k] = url if url.startswith("http") else ""
        elif isinstance(entry, str) and entry.startswith("http"):
            for k in ("tier90", "tier70", "tier50", "rest"):
                tiers[k] = entry
        return tiers

    @staticmethod
    def _tier_for_drop(drop):
        """Pick the score-tier channel from the Keepa drop percent (whichever
        window — 90/180/365/30d — the average came from)."""
        try:
            d = float(drop)
        except (TypeError, ValueError):
            return "rest"
        if d >= 90:
            return "tier90"
        if d >= 70:
            return "tier70"
        if d >= 50:
            return "tier50"
        return "rest"

    # ------------------------------------------------------------------ seeding

    def _seed_nodes(self):
        """Populate method_nodes from data/categories.json, one row per unique
        subcategory browse node, once per marketplace in self.marketplaces.
        Idempotent — safe to call on every startup.

        Only German target rows are retained; credential source rotation is
        independent of the target marketplace."""
        cats = _load_json(_CATEGORIES_PATH)
        tops = _load_json(_TOPCATEGORIES_PATH)

        # Dedupe by (searchIndex, browseNodeId) — several curated
        # keyword rows often share one subcategory node. Search browse-node-only
        # (no keyword) to cover the full subcategory rather than one query.
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

        valid_nodes = set(seen_nodes)
        removed_domains = delete_method_nodes_outside_marketplaces(
            self.marketplaces
        )
        if removed_domains:
            logger.info(
                "[METHOD] Removed %s non-German scanner nodes",
                removed_domains,
            )
        for marketplace in self.marketplaces:
            deleted = delete_stale_method_nodes(marketplace, 1, valid_nodes)
            if deleted:
                logger.info(
                    "[METHOD] Removed %s stale Method 1 nodes for %s",
                    deleted,
                    marketplace,
                )
            for (idx, node), label in seen_nodes.items():
                display = (top_by_index.get(idx) or {}).get("displayName") or idx
                enabled = 1 if idx in _DEFAULT_ENABLED_CATEGORIES else 0
                seed_method_node(
                    marketplace, idx, 1, node,
                    label=f"{display} → {label}", keywords=None, enabled=enabled,
                )

    # --------------------------------------------------------------- scan tick

    @staticmethod
    def _engine_enabled():
        """Read the shared switch at operation boundaries.

        A scan tick includes several slow external calls. Checking only once at
        the start meant a paused engine could continue scoring and posting the
        rest of an in-flight batch for minutes.
        """
        return bool(get_method_engine_state().get("enabled"))

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

        # Method 2 is retired. Historical rows can remain in existing databases,
        # but the scanner never schedules them.
        nodes = [
            n for n in get_method_nodes(enabled_only=True)
            if int(n["method"]) == 1 and n["marketplace"] == "DE"
        ]
        if not nodes:
            update_method_engine_state(running=0, last_heartbeat=now,
                                       current_target="No categories enabled")
            return None

        pointer = int(state.get("rr_pointer", 0) or 0) % len(nodes)
        node = nodes[pointer]
        credential_source = self.credential_marketplaces[
            int(state.get("rr_pointer", 0) or 0) % len(self.credential_marketplaces)
        ]
        human = (
            f'DE · {node["category"]} · credentials {credential_source} · '
            f'€{node.get("current_min_price", 0):.0f}+'
        )
        update_method_engine_state(
            running=1, rr_pointer=(pointer + 1) % len(nodes),
            last_heartbeat=now, current_target=human, last_error=None,
        )
        logger.info(f"[METHOD] tick: {human}")

        try:
            self._process_node(node, credential_source)
        except Exception as e:
            logger.exception(f"[METHOD] node processing error: {e}")
            update_method_engine_state(last_error=str(e)[:200], last_heartbeat=datetime.utcnow().isoformat())
        return node

    def _budget_exhausted(self):
        today_calls = sum(r.get("creators_api_calls") or 0 for r in get_method_stats())
        return today_calls >= self.daily_budget_requests

    # ------------------------------------------------------------- one node

    def _process_node(self, node, credential_source="DE"):
        marketplace = "DE"
        category = node["category"]
        method = int(node["method"])
        browse_node_id = node["browse_node_id"]
        min_price = float(node.get("current_min_price") or 0)

        self.searcher = self.searchers[credential_source]
        self.searcher.marketplace = "DE"

        result = self.searcher.diagnostic_search(
            search_index="All",
            keywords="",
            browse_node_id=browse_node_id,
            sort_by="Price:LowToHigh",
            min_saving_percent=0,
            min_price=min_price,
            max_price=self.max_price,
            item_count=10,
            max_pages=self.max_pages_per_tick,
            use_keepa=False,  # this engine applies its own flat Keepa gate below
            delivery_flags=["FulfilledByAmazon"],  # board note: "FBA: ON"
        )
        pages_scanned = int((result.get("response") or {}).get("pages_scanned") or 0)
        items = result.get("items") or []
        if pages_scanned:
            bump_method_stats(marketplace, category, method, creators_api_calls=pages_scanned)

        if not self._engine_enabled():
            return

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
            # Skip unless the price DROPPED since we last saw this ASIN —
            # same-or-higher prices are not a new deal, and re-posting them
            # was the main source of duplicate ASINs in the channels.
            if cached_price is not None and float(price) >= float(cached_price) - 0.01:
                skipped += 1
                continue
            to_process.append((it, asin, price))

        if skipped:
            bump_method_stats(marketplace, category, method, cache_skipped=skipped)
        if not to_process:
            return []

        bump_method_stats(marketplace, category, method, asins_scanned=len(to_process))

        # ---- Keepa: best available avg (90d, preferring longer 180d/365d
        # windows when 90d has no data; 30d only as a last resort), plus
        # percentage and currency gates ----
        if not self.keepa:
            return []

        if not self._engine_enabled():
            return []

        asins = [a for _, a, _ in to_process]
        try:
            avg_map = self.keepa.get_avg_batch(asins, domain=marketplace) or {}
            bump_method_stats(marketplace, category, method,
                              keepa_calls=max(1, (len(asins) + 99) // 100))
        except Exception as e:
            logger.error(f"[METHOD] Keepa batch error: {e}")
            return []

        if not self._engine_enabled():
            return []

        # Only suppress an unchanged price after Keepa answered successfully.
        # A token/network/API failure must remain eligible for the next scan.
        upsert_method_asin_cache_batch(marketplace, method,
                                       [(a, p) for _, a, p in to_process])

        survivors, rejected = [], 0
        for it, asin, price in to_process:
            if not self._engine_enabled():
                break
            avg, window = avg_map.get(asin) or (None, None)
            if not avg or avg <= 0:
                rejected += 1
                self._send_trash(method, it, asin, price,
                                 "No Keepa data (90/180/365/30d)", marketplace)
                continue
            currency_drop = float(avg) - price
            drop = currency_drop / float(avg) * 100
            if drop < self.keepa_drop_percent:
                rejected += 1
                self._send_trash(
                    method, it, asin, price,
                    f"Keepa drop {drop:.0f}% ({window}d) < {self.keepa_drop_percent:.0f}% threshold",
                    marketplace,
                )
                continue
            # Low-priced products cannot reasonably clear a fixed 30-unit drop
            # even when their percentage discount is excellent. Below the
            # configured average-price floor, the percentage gate above is
            # sufficient and the absolute currency gate does not apply.
            currency_gate_applies = DealFilters.absolute_drop_gate_applies(
                avg, self.min_drop_currency_avg_floor
            )
            if currency_gate_applies and currency_drop < self.min_drop_currency:
                rejected += 1
                self._send_trash(
                    method, it, asin, price,
                    f"Keepa drop €{currency_drop:.2f} ({window}d) < "
                    f"€{self.min_drop_currency:.2f} minimum",
                    marketplace,
                )
                continue
            it["_keepa_avg90"] = round(float(avg), 2)
            it["_keepa_drop"] = round(drop, 1)
            it["_keepa_window"] = window
            survivors.append((it, asin, price))

        if rejected:
            bump_method_stats(marketplace, category, method, keepa_rejected=rejected)
        return survivors

    def _send_trash(self, method, it, asin, price, reason, marketplace=None):
        """Post a Keepa/AI reject to the method's trash channel (if wired)."""
        trash_url = (self.webhook_by_method.get(method) or {}).get("trash", "")
        if not trash_url:
            return
        try:
            self.discord.send_trash(
                {
                    "asin": asin,
                    "title": DealFilters.extract_title(it) or "",
                    "image": DealFilters.extract_image(it),
                    "current_price": price,
                    "reject_reason": reason,
                    "marketplace": marketplace or self.marketplace,
                },
                webhook_url=trash_url,
            )
        except Exception as e:
            logger.warning(f"[METHOD] Trash Discord error for {asin}: {e}")

    def _score_and_post(self, marketplace, category, method, survivors):
        webhooks = self.webhook_by_method.get(method) or {}
        feed_user_id = METHOD_FEED_USER_IDS.get(method)

        ai_rejected = 0
        tier_rejected = 0
        posted = 0
        for it, asin, price in survivors:
            if not self._engine_enabled():
                break
            # Below the lowest % tier (50) -> trash, not the rest channel.
            # Checked BEFORE AI scoring so no AI call is spent on them.
            tier = self._tier_for_drop(it.get("_keepa_drop"))
            if tier == "rest":
                tier_rejected += 1
                drop = it.get("_keepa_drop") or 0
                window = it.get("_keepa_window") or 90
                self._send_trash(
                    method, it, asin, price,
                    f"Keepa drop {drop:.0f}% ({window}d) below 50% tier minimum",
                    marketplace,
                )
                continue

            title = DealFilters.extract_title(it) or ""
            estimate = {}
            try:
                if self.ai.enabled and title:
                    estimate = self.ai.estimate(
                        title, asin, marketplace=marketplace or self.marketplace,
                        price=price, category=category,
                    )
            except Exception as e:
                logger.warning(f"[METHOD] AI estimate error for {asin}: {e}")

            # Pause may have been pressed while the AI request was in flight.
            if not self._engine_enabled():
                break

            # All math/scoring is deterministic (deal_scoring.py); the AI only
            # supplied the price ranges above.
            scoring = compute_scores(estimate, price, self.config)
            score = scoring["overall_score"]

            if score < self.min_ai_score:
                ai_rejected += 1
                self._send_trash(
                    method, it, asin, price,
                    f"Score {score:.0f} < {self.min_ai_score:.0f} minimum — {scoring['ai_reason']}",
                    marketplace,
                )
                continue

            product = self._build_product(it, asin, price, category, marketplace, method, scoring)

            # Route by Keepa drop %: >=90 / >=70 / >=50 (below 50 was trashed above).
            webhook_url = webhooks.get(tier, "")

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

        if tier_rejected:
            bump_method_stats(marketplace, category, method, keepa_rejected=tier_rejected)
        if ai_rejected:
            bump_method_stats(marketplace, category, method, ai_rejected=ai_rejected)
        if posted:
            bump_method_stats(marketplace, category, method, posted=posted)

    @staticmethod
    def _build_product(item, asin, price, category, marketplace, method, scoring):
        listing = MethodScanner._first_listing(item)
        seller = DealFilters.extract_seller(listing)
        product = {
            "asin": asin,
            "title": DealFilters.extract_title(item) or "",
            "marketplace": marketplace,
            "current_price": price,
            "savings_percent": item.get("_keepa_drop"),
            "category": f"{category} (Method {method})",
            "search_category": category,
            "seller_name": seller.get("seller_name") or "Unknown",
            "seller_id": seller.get("seller_id"),
            "keepa_avg_90": item.get("_keepa_avg90"),
            "keepa_drop_percent": item.get("_keepa_drop"),
            "keepa_window": item.get("_keepa_window", 90),
            "image": DealFilters.extract_image(item),
        }
        product.update(to_product_fields(scoring))
        return product

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
        logger.info(
            "[METHOD] Starting German category loop (cadence %ss, credentials %s, "
            "daily budget %s)",
            self.interval,
            self.scanner.credential_marketplaces,
            self.scanner.daily_budget_requests,
        )
        while True:
            tick_started = time.monotonic()
            try:
                self.scanner.run_once()
            except Exception as e:
                logger.exception(f"[METHOD] tick failed: {e}")
                try:
                    update_method_engine_state(last_error=str(e)[:200],
                                               last_heartbeat=datetime.utcnow().isoformat())
                except Exception:
                    pass
            # Maintain the configured start-to-start cadence. API pagination
            # time counts toward the interval instead of being added on top.
            elapsed = time.monotonic() - tick_started
            time.sleep(max(0.0, self.interval - elapsed))
