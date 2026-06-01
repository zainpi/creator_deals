"""Continuous, category-aware ASIN discovery engine.

Runs in a separate worker process (worker.py). Sweeps a wide matrix of
marketplace x category x keyword x sort x page to surface a steady flow of new
ASINs, judges price attractiveness relative to each category's learned typical
price, confirms a category-scaled euro-off vs the Keepa 90-day average, scores
with AI, and publishes to the shared global feed.
"""

import time
import logging
from datetime import datetime

from creators_search import (
    CreatorsSearch,
    DEAL_KEYWORDS_BY_MARKETPLACE,
    DEAL_SEARCH_INDICES,
)
from keepa_service import KeepaService
from ai_scoring import AIScorer
from discord_alerts import DiscordAlerts
from filters import DealFilters, set_blocked_categories
from database import (
    GLOBAL_FEED_USER_ID,
    insert_product,
    get_scanner_state, update_scanner_state,
    get_category_baselines, bump_category_baseline,
    seen_recently, mark_seen,
)

logger = logging.getLogger(__name__)

# Fixed number of keyword slots used by the cursor decode. Locale keyword lists
# vary in length; we modulo into the actual list so every keyword is reachable.
_KEYWORD_SLOTS = 5


class ContinuousScanner:
    """One scan tick = one Creators search + category-aware Keepa/AI pipeline."""

    def __init__(self, config):
        self.config = config

        scanner_cfg = config.get("scanner", {}) or {}
        pricing_cfg = config.get("pricing", {}) or {}

        self.marketplaces = [str(m).upper() for m in
                             (scanner_cfg.get("marketplaces") or ["DE", "GB", "FR", "ES", "IT"])]
        self.sort_rotation = scanner_cfg.get("sort_rotation") or \
            ["Featured", "Price:LowToHigh", "NewestArrivals"]
        self.pages_to_search = max(1, int(scanner_cfg.get("pages_to_search", 5)))
        self.scan_interval = max(1, int(scanner_cfg.get("scan_interval_seconds", 15)))
        self.cooldown_hours = float(scanner_cfg.get("cooldown_hours", 48))

        self.min_euro_off_default = float(pricing_cfg.get("min_euro_off_default", 50))
        self.min_euro_off_floor = float(pricing_cfg.get("min_euro_off_floor", 8))
        self.euro_off_pct = float(pricing_cfg.get("euro_off_pct_of_typical", 0.15))
        self.min_baseline_samples = int(pricing_cfg.get("min_baseline_samples", 3))
        self.worth_keepa_factor = float(pricing_cfg.get("worth_keepa_factor", 0.6))
        # Category-relative path: keep an item if it's at least this % below its
        # Keepa 90-day average, regardless of category or learned baseline. This is
        # what lets cheap-category deals through (a 20% drop is good for a toy even
        # though it's nowhere near €50 off).
        self.min_drop_percent = float(pricing_cfg.get("min_drop_percent", 20))

        set_blocked_categories(config)

        self.searcher = CreatorsSearch(config)

        # Keepa is the heart of the gate; create it only if enabled + key present.
        self.keepa = None
        keepa_key = config.get("keepa", {}).get("api_key", "")
        keepa_enabled = config.get("keepa", {}).get("enabled", True)
        if keepa_enabled and keepa_key:
            try:
                self.keepa = KeepaService(keepa_key)
            except Exception as e:
                logger.error(f"[SCANNER] Keepa init failed: {e}")
        self.keepa_active = self.keepa is not None
        if not self.keepa_active:
            logger.warning("[SCANNER] Keepa is OFF — items will be published without the "
                           "euro-off gate. Enable Keepa for category-scaled discount filtering.")

        self.ai = AIScorer(config)
        self.min_ai_score = config.get("ai", {}).get("minimum_score", 7)
        self.discord = DiscordAlerts(config.get("discord", {}).get("webhook_url", ""))

        # Cumulative counters surfaced to the dashboard.
        self.scanned_total = 0
        self.kept_total = 0

    # ------------------------------------------------------------------ cursor

    def _dimensions(self):
        """Radix sizes for the mixed-radix tick cursor (fastest -> slowest)."""
        return (
            len(self.marketplaces),
            len(DEAL_SEARCH_INDICES),
            _KEYWORD_SLOTS,
            len(self.sort_rotation),
            self.pages_to_search,
        )

    def _decode_tick(self, tick):
        """Map a monotonic tick to a unique (marketplace, index, keyword, sort, page)."""
        M, I, K, S, P = self._dimensions()
        total = max(1, M * I * K * S * P)
        i = tick % total
        mk_idx = i % M; i //= M
        idx_idx = i % I; i //= I
        kw_slot = i % K; i //= K
        sort_idx = i % S; i //= S
        page = (i % P) + 1

        marketplace = self.marketplaces[mk_idx]
        search_index = DEAL_SEARCH_INDICES[idx_idx]
        kw_list = DEAL_KEYWORDS_BY_MARKETPLACE.get(marketplace, ["deal"])
        keyword = kw_list[kw_slot % len(kw_list)]
        sort_by = self.sort_rotation[sort_idx]
        return {
            "marketplace": marketplace,
            "search_index": search_index,
            "keyword": keyword,
            "sort_by": sort_by,
            "page": page,
        }

    # ------------------------------------------------------------- pricing brain

    def _required_euro_off(self, baseline):
        """Category-scaled euro-off threshold vs the 90-day average."""
        if not baseline or baseline.get("sample_count", 0) < self.min_baseline_samples:
            return self.min_euro_off_default  # cold start: strict default
        typical = baseline.get("avg90_mean") or 0
        scaled = typical * self.euro_off_pct
        return max(self.min_euro_off_floor, min(self.min_euro_off_default, scaled))

    def _worth_keepa(self, baseline, current_price):
        """Cheap pre-filter: only spend a Keepa lookup if the price is plausibly a
        good deal for this category. Cold start (immature baseline) -> always check.
        Otherwise check anything priced at or below the category's typical 90d avg."""
        if not baseline or baseline.get("sample_count", 0) < self.min_baseline_samples:
            return True
        typical = baseline.get("avg90_mean") or 0
        if typical <= 0:
            return True
        return current_price <= typical

    # --------------------------------------------------------------- scan tick

    def run_once(self):
        """Execute one scan tick. Returns the decoded target (for logging)."""
        state = get_scanner_state()
        now = datetime.utcnow().isoformat()

        if not state.get("enabled", 1):
            update_scanner_state(running=0, last_heartbeat=now)
            return None

        tick = int(state.get("tick", 0) or 0)
        target = self._decode_tick(tick)
        mk = target["marketplace"]
        mk_domain = "GB" if mk in ("GB", "UK") else mk
        human_target = f'{mk} · {target["search_index"]} · "{target["keyword"]}" · p{target["page"]} · {target["sort_by"]}'
        update_scanner_state(running=1, last_heartbeat=now, current_target=human_target, last_error=None)
        logger.info(f"[SCANNER] tick {tick}: {human_target}")

        # 1 search call (rate-limited inside search_items)
        self.searcher.marketplace = mk
        try:
            self.searcher._load_credentials()
        except Exception:
            pass
        try:
            items = self.searcher.search_items(
                page=target["page"],
                search_index=target["search_index"],
                sort_by=target["sort_by"],
                keywords=target["keyword"],
                min_saving_percent=1,      # API minimum is 1; we gate for real via Keepa, not API %
                max_price=100000.0,
            )
        except Exception as e:
            logger.error(f"[SCANNER] search error: {e}")
            update_scanner_state(tick=tick + 1, last_error=str(e)[:200], last_heartbeat=now)
            return target

        baselines = get_category_baselines(mk)

        # ---- cheap pre-pass: dedup/cooldown + category pre-filter ----
        candidates = []  # (norm, asin, price, listing, category, seller_id)
        for raw in (items or []):
            try:
                norm = self.searcher._normalize_item(raw)
                if not isinstance(norm, dict):
                    continue
            except Exception:
                continue
            norm["_marketplace"] = mk
            norm["_page"] = target["page"]

            asin = norm.get("ASIN")
            if not asin:
                continue
            category = DealFilters.extract_category(norm)
            if not DealFilters.category_allowed(category):
                continue
            listing = self._first_listing(norm)
            price = DealFilters.extract_price(listing) if listing else None
            if not price:
                continue

            self.scanned_total += 1
            if seen_recently(mk, asin, self.cooldown_hours):
                continue
            mark_seen(mk, asin)

            if self.keepa_active and not self._worth_keepa(baselines.get(category), price):
                continue

            seller_id = DealFilters.extract_seller(listing).get("seller_id")
            candidates.append((norm, asin, price, listing, category, seller_id))

        # ---- batched Keepa for survivors ----
        keepa_map, rating_map = {}, {}
        if self.keepa_active and candidates:
            try:
                keepa_map = self.keepa.validate_deals_batch(
                    [(c[1], c[2]) for c in candidates], domain=mk_domain)
            except Exception as e:
                logger.error(f"[SCANNER] Keepa batch error: {e}")
            try:
                rating_map = self.keepa.get_seller_ratings_batch(
                    [c[5] for c in candidates if c[5]], domain=mk_domain)
            except Exception as e:
                logger.warning(f"[SCANNER] Seller rating batch error: {e}")

        # ---- gate + AI + save ----
        kept_this_tick = 0
        best_drop, best_eur = 0.0, 0.0   # near-miss tracking for tuning visibility
        for norm, asin, price, listing, category, seller_id in candidates:
            kd = keepa_map.get(asin) if self.keepa_active else None

            if self.keepa_active:
                if not kd or not kd.get("avg90"):
                    continue  # can't validate without 90-day data
                avg90 = kd["avg90"]
                bump_category_baseline(mk, category, avg90)
                # fold into in-memory baseline so the threshold adapts within the tick
                self._fold_baseline(baselines, category, avg90)

                euro_off = avg90 - price
                drop_pct = kd.get("drop_percent")
                if drop_pct is None:
                    drop_pct = (euro_off / avg90 * 100) if avg90 else 0
                required_eur = self._required_euro_off(baselines.get(category))
                # Keep on EITHER a big absolute euro drop OR a strong category-relative %.
                if not (euro_off >= required_eur or drop_pct >= self.min_drop_percent):
                    best_drop = max(best_drop, drop_pct)
                    best_eur = max(best_eur, euro_off)
                    continue

            product = self._build_product(norm, asin, price, listing, category, kd,
                                          rating_map.get(seller_id) if self.keepa_active else 0)
            try:
                insert_product(product)
                kept_this_tick += 1
                self.kept_total += 1
            except Exception as e:
                logger.error(f"[SCANNER] DB insert error for {asin}: {e}")
                continue

            # Discord on strong AI score
            try:
                if self.discord and (product.get("ai_score") or 0) >= self.min_ai_score:
                    self.discord.send(product)
            except Exception as e:
                logger.warning(f"[SCANNER] Discord error for {asin}: {e}")

        update_scanner_state(
            tick=tick + 1,
            scanned_count=self.scanned_total,
            kept_count=self.kept_total,
            last_heartbeat=datetime.utcnow().isoformat(),
        )
        msg = (f"[SCANNER] tick {tick}: {len(items or [])} items, "
               f"{len(candidates)} candidates, {kept_this_tick} kept")
        if candidates and kept_this_tick == 0 and self.keepa_active:
            msg += (f" — best near-miss {best_drop:.0f}% / €{best_eur:.0f} off "
                    f"(need {self.min_drop_percent:.0f}% or scaled €)")
        logger.info(msg)
        return target

    def _build_product(self, norm, asin, price, listing, category, kd, seller_rating):
        seller_data = DealFilters.extract_seller(listing)
        title = DealFilters.extract_title(norm)

        ai_score = 5.0
        ai_reason = ""
        try:
            if self.ai and self.ai.enabled and title:
                ai_score = float(self.ai.score_deal(title, asin))
            elif not (self.ai and self.ai.enabled):
                ai_reason = "AI disabled"
        except Exception as e:
            logger.warning(f"[SCANNER] AI error for {asin}: {e}")

        product = {
            "user_id": GLOBAL_FEED_USER_ID,
            "asin": asin,
            "title": title,
            "marketplace": norm.get("_marketplace"),
            "current_price": price,
            "savings_percent": DealFilters.extract_savings_percent(listing),
            "category": category,
            "seller_name": seller_data.get("seller_name"),
            "seller_id": seller_data.get("seller_id"),
            "seller_rating": seller_rating if seller_rating is not None else (0 if not self.keepa_active else None),
            "keepa_avg_90": (kd or {}).get("avg90"),
            "keepa_drop_percent": (kd or {}).get("drop_percent"),
            "ai_score": ai_score,
            "ai_reason": ai_reason,
            "image": DealFilters.extract_image(norm),
        }
        page_found = norm.get("_page")
        if page_found is not None:
            product["page_found"] = int(page_found)
        return product

    @staticmethod
    def _first_listing(item: dict) -> dict:
        listings = (item.get("OffersV2", {}) or {}).get("Listings", []) or []
        return listings[0] if listings else {}

    @staticmethod
    def _fold_baseline(baselines, category, avg90):
        b = baselines.get(category)
        if not b:
            baselines[category] = {"sample_count": 1, "avg90_sum": avg90, "avg90_mean": avg90}
            return
        b["sample_count"] = b.get("sample_count", 0) + 1
        b["avg90_sum"] = (b.get("avg90_sum") or 0) + avg90
        b["avg90_mean"] = b["avg90_sum"] / b["sample_count"]


class ScanLoop:
    """Drives ContinuousScanner.run_once() forever, sleeping between ticks."""

    def __init__(self, config):
        self.config = config
        self.scanner = ContinuousScanner(config)
        self.interval = self.scanner.scan_interval

    def run_forever(self):
        logger.info("[SCANNER] Starting continuous scan loop "
                    f"(interval {self.interval}s, marketplaces {self.scanner.marketplaces})")
        while True:
            try:
                self.scanner.run_once()
            except Exception as e:
                logger.exception(f"[SCANNER] tick failed: {e}")
                try:
                    update_scanner_state(last_error=str(e)[:200],
                                         last_heartbeat=datetime.utcnow().isoformat())
                except Exception:
                    pass
            time.sleep(self.interval)
