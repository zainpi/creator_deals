import logging
import os
import threading
from datetime import datetime

from flask import Flask, render_template, jsonify, request

from config_loader import load_config
from scheduler import DealScheduler
from testing_stats import TestingStats
from database import (
    init_db, reset_db, GLOBAL_FEED_USER_ID, METHOD_FEED_USER_IDS,
    get_products_for_user, get_stats_for_user, clear_products_for_user,
    get_user_preferences, save_user_preferences,
    set_search_job, get_search_job,
    set_batch_job, get_batch_job,
    get_scanner_state, update_scanner_state,
    get_method_nodes, set_method_nodes_enabled,
    get_method_stats, get_method_engine_state, update_method_engine_state,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')

# Load server config (admin-only, used as defaults for AI/Keepa/Discord)
config = load_config()

# Init DB at import time so gunicorn workers always have the schema ready
try:
    init_db()
    logger.info("[APP] Database initialized")
except Exception as e:
    logger.error(f"[APP] Database init error: {e}")


# ==================== Routes ====================

@app.route("/")
def index():
    return render_template("index.html")


# ==================== Per-user products & stats ====================

@app.route("/api/products")
def api_products():
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify([])
    return jsonify(get_products_for_user(user_id))


@app.route("/api/stats")
def api_stats():
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({"total_discovered": 0, "total_posted": 0})
    return jsonify(get_stats_for_user(user_id))


@app.route("/api/clear_products", methods=["POST"])
def api_clear_products():
    user_id = (request.json or {}).get("user_id", "")
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    try:
        clear_products_for_user(user_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== Per-user preferences ====================

@app.route("/api/preferences", methods=["GET", "POST"])
def api_preferences():
    if request.method == "GET":
        user_id = request.args.get("user_id", "")
        if not user_id:
            return jsonify({})
        return jsonify(get_user_preferences(user_id))

    # POST
    data = request.json or {}
    user_id = data.get("user_id", "")
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    try:
        save_user_preferences(user_id, data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== On-demand search ====================

@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.json or {}
    user_id = data.get("user_id", "")
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400

    keywords = str(data.get("keywords", "")).strip()
    marketplaces = ["DE"]
    pages = max(1, min(int(data.get("pages", 1)), 20))
    min_saving = int(data.get("min_saving", config.get("amazon", {}).get("min_saving_percent", 50)))
    max_price = float(data.get("max_price", config.get("amazon", {}).get("max_price", 450)))

    # Creators API sortBy — validate against the supported set
    VALID_SORT = {"Featured", "Price:LowToHigh", "Price:HighToLow", "AvgCustomerReviews", "NewestArrivals"}
    sort_by = str(data.get("sort_by") or config.get("amazon", {}).get("sort_by") or "Featured").strip()
    if sort_by not in VALID_SORT:
        sort_by = "Featured"

    # Per-user enrichment toggles. Default to the server config value when the
    # request omits them. These only enable/disable enrichment + Discord posting;
    # items are never dropped just because Keepa or AI is off.
    use_keepa = bool(data.get("use_keepa", config.get("keepa", {}).get("enabled", False)))
    use_ai = bool(data.get("use_ai", config.get("ai", {}).get("enabled", True)))

    filters = {
        "use_filters":       bool(data.get("use_filters", True)),
        "min_saving":        int(data.get("f_min_saving", 0)),
        "min_ai_score":      float(data.get("f_min_ai_score", 0)),
        "min_seller_rating": float(data.get("f_min_seller_rating", 0)),
        "min_price":         float(data.get("f_min_price", 0)),
        "max_price":         float(data.get("f_max_price", 0)),
    }

    # Build per-request config inheriting server defaults.
    # Copy each mutated sub-section so we never mutate the shared global config.
    search_config = dict(config)
    search_config["amazon"] = dict(config.get("amazon", {}))
    search_config["amazon"]["keywords"] = keywords
    search_config["amazon"]["min_saving_percent"] = min_saving
    search_config["amazon"]["max_price"] = max_price
    # Make the user's sortBy authoritative (overrides the server default in search_items)
    search_config["amazon"]["sort_by"] = sort_by
    # Apply per-user enrichment toggles
    search_config["keepa"] = dict(config.get("keepa", {}))
    search_config["keepa"]["enabled"] = use_keepa
    search_config["ai"] = dict(config.get("ai", {}))
    search_config["ai"]["enabled"] = use_ai

    # Don't start a second search for the same user while one is running.
    existing = get_search_job(user_id)
    if existing.get("status") == "running":
        return jsonify({"success": True, "started": False,
                        "message": "A search is already running"}), 202

    search_kwargs = dict(
        keywords=keywords,
        marketplaces=marketplaces,
        pages=pages,
        min_saving=min_saving,
        max_price=max_price,
        filters=filters,
        sort_by=sort_by,
    )

    # Run the search in the background so the HTTP request returns immediately
    # (avoids the host router timeout). Items are saved to the DB as they're
    # processed, so the page can poll /api/products and show them as they land.
    set_search_job(user_id, status="running", found=0, api_calls=0,
                   error=None, started_at=datetime.utcnow().isoformat(), finished_at=None)
    threading.Thread(
        target=_run_search_job,
        args=(user_id, search_config, search_kwargs),
        daemon=True,
    ).start()

    return jsonify({"success": True, "started": True})


def _run_search_job(user_id, search_config, search_kwargs):
    """Background worker: run the search and record final status in the DB."""
    try:
        stats = TestingStats()
        scheduler = DealScheduler(search_config, stats)
        results = scheduler.search_for_user(user_id=user_id, **search_kwargs)
        set_search_job(user_id, status="done", found=len(results),
                       api_calls=stats.api_calls, error=None,
                       finished_at=datetime.utcnow().isoformat())
    except Exception as e:
        logger.exception(f"[APP] Search error for user {user_id}: {e}")
        set_search_job(user_id, status="error", error=str(e),
                       finished_at=datetime.utcnow().isoformat())


@app.route("/api/search_status")
def api_search_status():
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({})
    return jsonify(get_search_job(user_id))


# ==================== Continuous global feed (worker-driven) ====================

@app.route("/api/feed")
def api_feed():
    """The shared, continuously-scanned discovery feed."""
    products = get_products_for_user(GLOBAL_FEED_USER_ID)
    category = (request.args.get("category") or "").strip().lower()
    if category:
        products = [p for p in products if (p.get("category") or "").lower() == category]
    try:
        limit = int(request.args.get("limit", 0))
    except Exception:
        limit = 0
    if limit > 0:
        products = products[:limit]
    return jsonify(products)


@app.route("/api/scanner_status")
def api_scanner_status():
    state = get_scanner_state()
    stats = get_stats_for_user(GLOBAL_FEED_USER_ID)
    state["feed_total"] = stats.get("total_discovered", 0)
    state["feed_posted"] = stats.get("total_posted", 0)
    return jsonify(state)


@app.route("/api/scanner_control", methods=["POST"])
def api_scanner_control():
    action = (request.json or {}).get("action", "")
    if action == "pause":
        update_scanner_state(enabled=0)
    elif action == "resume":
        update_scanner_state(enabled=1)
    else:
        return jsonify({"success": False, "error": "action must be 'pause' or 'resume'"}), 400
    return jsonify({"success": True, "state": get_scanner_state()})


# ==================== Test endpoint (unchanged) ====================

@app.route("/api/test", methods=["POST"])
def api_test():
    import time
    from creators_search import CreatorsSearch
    from keepa_service import KeepaService
    from ai_scoring import AIScorer
    from deal_scoring import compute_scores

    try:
        params = request.json
        test_config = dict(config)
        test_config["amazon"]["marketplace"] = "DE"
        test_config["amazon"]["min_saving_percent"] = params.get("min_saving", 50)
        test_config["amazon"]["max_price"] = params.get("max_price", 450)
        test_config["amazon"]["keywords"] = params.get("keywords", test_config.get("amazon", {}).get("keywords", ""))
        test_config["filters"]["min_keepa_drop_percent"] = params.get("min_drop", 35)
        test_config["filters"]["min_rating"] = params.get("min_rating", 4.0)
        test_config["filters"]["min_review_count"] = params.get("min_reviews", 10)
        test_config["ai"]["enabled"] = params.get("use_ai", True)
        test_config["ai"]["minimum_score"] = params.get("min_ai_score", 50)
        if "keepa" not in test_config:
            test_config["keepa"] = {}
        test_config["keepa"]["enabled"] = params.get("use_keepa", test_config.get("keepa", {}).get("enabled", True))

        creators = CreatorsSearch(test_config)
        keepa = KeepaService(test_config) if test_config.get("keepa", {}).get("enabled", True) else None
        ai = AIScorer(test_config)

        start_page = params.get("page_start", 1)
        end_page = params.get("page_end", 1)
        start_time = time.time()

        found_count = 0
        keepa_passed = 0
        ai_passed = 0
        results = []
        errors = []

        for page_num in range(start_page, end_page + 1):
            try:
                items = creators.search_items(
                    page=page_num,
                    min_saving_percent=params.get("min_saving", 50),
                    max_price=params.get("max_price", 450),
                )
                found_count += len(items)
                for item in items:
                    asin = item.get("ASIN", "")
                    title = item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", "")
                    listings = item.get("OffersV2", {}).get("Listings", [])
                    price_amt = None
                    if listings:
                        price = listings[0].get("Price", {})
                        price_amt = price.get("Amount")

                    keepa_drop = None
                    keepa_passed_item = False
                    if price_amt is not None and keepa is not None:
                        try:
                            kp = keepa.validate_deal(asin, float(price_amt), domain=test_config.get("amazon", {}).get("marketplace", "DE"))
                            if kp:
                                keepa_passed += 1
                                keepa_passed_item = True
                                keepa_drop = kp.get("drop_percent", 0)
                        except Exception as e:
                            logger.warning(f"[TEST] Keepa error for {asin}: {e}")

                    ai_score = 50.0
                    scoring = None
                    if test_config["ai"]["enabled"] and keepa_passed_item and title:
                        try:
                            buy = float(price_amt) if price_amt is not None else None
                            estimate = ai.estimate(
                                title, asin,
                                marketplace=test_config["amazon"]["marketplace"],
                                price=buy,
                            )
                            scoring = compute_scores(estimate, buy, test_config)
                            ai_score = scoring["overall_score"]
                            if ai_score >= test_config["ai"]["minimum_score"]:
                                ai_passed += 1
                        except Exception as e:
                            logger.warning(f"[TEST] AI error for {asin}: {e}")

                    row = {
                        "asin": asin,
                        "title": title[:80],
                        "price": float(price_amt) if price_amt is not None else None,
                        "keepa_drop": keepa_drop,
                        "ai_score": ai_score,
                        "page": page_num,
                        "url": f"https://www.amazon.{creators._tld_for_marketplace(test_config['amazon']['marketplace'])}/dp/{asin}",
                    }
                    if scoring:
                        row.update({
                            "buying_score": scoring["buying_score"],
                            "resell_score": scoring["resell_score"],
                            "estimated_profit": scoring["estimated_profit"],
                            "discount_pct": scoring["discount_pct"],
                            "retail_low": scoring["retail_low"],
                            "retail_high": scoring["retail_high"],
                            "resale_low": scoring["resale_low"],
                            "resale_high": scoring["resale_high"],
                            "ai_reason": scoring["ai_reason"],
                        })
                    results.append(row)
            except Exception as e:
                error_msg = f"Page {page_num}: {str(e)[:100]}"
                errors.append(error_msg)
                logger.error(f"[TEST] {error_msg}")

        elapsed = time.time() - start_time
        return jsonify({
            "success": True,
            "found": found_count,
            "keepa_passed": keepa_passed,
            "ai_passed": ai_passed,
            "pages_scanned": end_page - start_page + 1,
            "time": round(elapsed, 2),
            "errors": errors,
            "results": results[:50],
        })
    except Exception as e:
        logger.error(f"[APP] Test scan error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== Category-ID raw diagnostic search ====================
#
# These endpoints power the "Category ID Test (Raw JSON)" panel. They hit the
# Amazon Creators API directly and return the EXACT request payload + raw
# response, with NO Keepa / AI enrichment — so you can verify the API is
# working and see precisely what each input produces.

import json as _json
import time as _time

_CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "data", "categories.json")


def _load_categories():
    try:
        with open(_CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        logger.error(f"[APP] Could not load categories.json: {e}")
        return []


def _creators_for_marketplace(marketplace):
    """Build a CreatorsSearch bound to Germany."""
    from creators_search import CreatorsSearch
    diag_config = dict(config)
    diag_config["amazon"] = dict(config.get("amazon", {}))
    diag_config["amazon"]["marketplace"] = "DE"
    return CreatorsSearch(diag_config)


# Creators API SortBy — all 6 documented options
VALID_SORT_RAW = {
    "Featured", "Price:LowToHigh", "Price:HighToLow",
    "AvgCustomerReviews", "NewestArrivals", "Relevance",
}


def _build_keepa_service():
    """Build a KeepaService from config, or None if unavailable (no key etc.)."""
    try:
        from keepa_service import KeepaService
        return KeepaService(config)
    except Exception as e:
        logger.warning(f"[APP] Keepa unavailable for raw search: {e}")
        return None


def _run_diagnostic(cs, params):
    """Call diagnostic_search with sanitised params. Returns the result dict.

    When `use_keepa` is set, the saving threshold is enforced against the Keepa
    90-day average price (KeepaBot-master method). Otherwise "option 3" applies:
    listings Amazon returns with no savingBasis pass through instead of being
    dropped.
    """
    sort_by = str(params.get("sort_by") or "Featured").strip()
    if sort_by not in VALID_SORT_RAW:
        sort_by = "Featured"

    use_keepa = bool(params.get("use_keepa"))
    keepa = _build_keepa_service() if use_keepa else None
    domain = "DE"

    # deliveryFlags: accept an explicit list, or the `use_fba` toggle shortcut.
    # None => fall back to config default (unchanged behaviour when toggle is off).
    delivery_flags = params.get("delivery_flags")
    if delivery_flags is None and params.get("use_fba"):
        delivery_flags = ["FulfilledByAmazon"]

    return cs.diagnostic_search(
        search_index=str(params.get("search_index") or "All").strip(),
        keywords=str(params.get("keywords") or "").strip(),
        browse_node_id=(str(params.get("browse_node_id")).strip() or None)
                        if params.get("browse_node_id") else None,
        sort_by=sort_by,
        min_saving_percent=int(params.get("min_saving") or 0),
        min_price=float(params.get("min_price") or 0),
        max_price=float(params.get("max_price") or 450),
        item_page=int(params.get("item_page") or 1),
        item_count=(int(params["item_count"]) if params.get("item_count") else None),
        use_keepa=use_keepa,
        keepa=keepa,
        keepa_domain=domain,
        delivery_flags=delivery_flags,
    )


@app.route("/api/categories", methods=["GET"])
def api_categories():
    """Serve the category test table (searchIndex, keywords, browseNodeId, ...)."""
    return jsonify(_load_categories())


@app.route("/api/topcategories", methods=["GET"])
def api_topcategories():
    """Serve the top-level 'Pick a Category' table (searchIndex, German
    displayName, and any available parent browse-node metadata)."""
    return jsonify(_load_topcategories())


@app.route("/api/raw_search", methods=["POST"])
def api_raw_search():
    """
    Single category-ID diagnostic search. Returns the exact request payload the
    Creators API received plus the raw response and normalized items. No Keepa/AI.
    """
    params = dict(request.json or {})
    params["marketplace"] = "DE"
    try:
        cs = _creators_for_marketplace("DE")
        result = _run_diagnostic(cs, params)
        result["api_calls"] = cs.api_calls
        return jsonify(result)
    except Exception as e:
        logger.exception(f"[APP] raw_search error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/batch_test", methods=["POST"])
def api_batch_test():
    """
    Kick off the raw diagnostic across many categories as a background job.

    The batch loops through up to ~65 categories, each hitting the Amazon
    Creators API — far too long for one request/response cycle under the host
    router's 30s hard timeout. So we start it in a daemon thread (mirroring the
    /api/search job pattern), record progress in the DB, and let the client poll
    /api/batch_status. This endpoint returns immediately.

    Body:
      marketplace, sort_by, min_price, max_price, item_page, item_count
      min_saving        — optional global override; if omitted each category's
                          own minSavingPercent from the table is used
      category_ids      — optional list of category ids to run; omit/empty = all
      use_category_saving — bool (default True): use each row's minSavingPercent
    """
    params = dict(request.json or {})
    params["marketplace"] = "DE"

    # Don't start a second batch while one is already running.
    existing = get_batch_job()
    if existing.get("status") == "running":
        return jsonify({"success": True, "started": False,
                        "message": "A batch is already running"}), 202

    set_batch_job(status="running", total=0, completed=0, error=None,
                  result=None, started_at=datetime.utcnow().isoformat(),
                  finished_at=None)
    threading.Thread(target=_run_batch_job, args=(params,), daemon=True).start()
    return jsonify({"success": True, "started": True})


def _run_batch_job(params):
    """Background worker: run the category batch and store the result JSON.

    Everything is wrapped so any failure is recorded as job status='error' with
    a message — the client always polls JSON, never an HTML 500 page.
    """
    try:
        cats = _load_categories()

        wanted = params.get("category_ids") or []
        if wanted:
            wanted_set = {int(x) for x in wanted}
            cats = [c for c in cats if c.get("id") in wanted_set]

        use_cat_saving = bool(params.get("use_category_saving", True))
        override_saving = params.get("min_saving")

        marketplace = "DE"
        cs = _creators_for_marketplace(marketplace)

        set_batch_job(total=len(cats), completed=0)

        rows = []
        started = _time.time()
        for c in cats:
            min_saving = (c.get("minSavingPercent", 50)
                          if use_cat_saving and override_saving in (None, "")
                          else int(override_saving or 0))
            p = {
                "search_index": c.get("searchIndex", "All"),
                "keywords": c.get("keywords", ""),
                "browse_node_id": c.get("browseNodeId", ""),
                "sort_by": params.get("sort_by", "Featured"),
                "min_saving": min_saving,
                "min_price": params.get("min_price", 0),
                "max_price": params.get("max_price", 450),
                "item_page": params.get("item_page", 1),
                "item_count": params.get("item_count"),
            }
            try:
                res = _run_diagnostic(cs, p)
            except Exception as e:
                res = {"ok": False, "error": str(e), "items": [], "response": None,
                       "request": {"payload": p}}

            resp = res.get("response") or {}
            # Compact per-item view for the table
            items_view = []
            for it in (res.get("items") or []):
                listing = ((it.get("OffersV2") or {}).get("Listings") or [{}])[0]
                price = (listing.get("Price") or {}).get("Amount")
                items_view.append({
                    "asin": it.get("ASIN"),
                    "title": (it.get("ItemInfo", {}).get("Title", {}).get("DisplayValue") or "")[:90],
                    "price": price,
                    "saving_pct": listing.get("SavingBasis"),
                    "category": it.get("Category"),
                })

            rows.append({
                "id": c.get("id"),
                "searchIndex": c.get("searchIndex"),
                "keywords": c.get("keywords"),
                "browseNodeId": c.get("browseNodeId"),
                "priceNote": c.get("priceNote"),
                "minSavingUsed": min_saving,
                "ok": res.get("ok", False),
                "status": resp.get("status"),
                "found": resp.get("item_count", len(res.get("items") or [])),
                "elapsed_ms": resp.get("elapsed_ms"),
                "error": res.get("error"),
                "request_payload": (res.get("request") or {}).get("payload"),
                "items": items_view,
            })
            set_batch_job(completed=len(rows))

        result = {
            "ok": True,
            "marketplace": str(marketplace).upper(),
            "count": len(rows),
            "total_found": sum(r["found"] for r in rows),
            "elapsed_s": round(_time.time() - started, 1),
            "api_calls": cs.api_calls,
            "rows": rows,
        }
        set_batch_job(status="done", result=_json.dumps(result),
                      finished_at=datetime.utcnow().isoformat())
    except Exception as e:
        logger.exception(f"[APP] Batch job error: {e}")
        set_batch_job(status="error", error=str(e),
                      finished_at=datetime.utcnow().isoformat())


@app.route("/api/batch_status")
def api_batch_status():
    """Poll target for the background batch job. Always returns JSON.

    Shape: {status, total, completed, error, started_at, finished_at, result}
    where `result` is the parsed batch payload once status='done' (else null).
    """
    job = get_batch_job()
    raw = job.pop("result", None)
    try:
        job["result"] = _json.loads(raw) if raw else None
    except Exception:
        job["result"] = None
    return jsonify(job)


# ==================== Live category scanner ====================
#
# Controls + status for method_scanner.py, which runs in the worker process
# (see worker.py). This app process only reads/writes DB state — it never runs
# the engine's ticks itself.

_TOPCATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "data", "topcategories.json")


def _load_topcategories():
    try:
        with open(_TOPCATEGORIES_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        logger.error(f"[APP] Could not load topcategories.json: {e}")
        return []


@app.route("/api/method_test/categories")
def api_method_test_categories():
    """Every top-level category with its subcategory browse-node count and
    whether it is switched on in the round robin.
    Nodes exist per-marketplace now, so this reports on ONE marketplace at a
    time: pass ?marketplace=GB (defaults to method_test.marketplace)."""
    marketplace = "DE"
    tops = {t.get("searchIndex"): t for t in _load_topcategories() if t.get("searchIndex")}
    nodes = get_method_nodes(marketplace=marketplace)

    groups = {}  # (category, method) -> {node_count, enabled_count}
    for n in nodes:
        key = (n["category"], int(n["method"]))
        g = groups.setdefault(key, {"node_count": 0, "enabled_count": 0})
        g["node_count"] += 1
        if n.get("enabled"):
            g["enabled_count"] += 1

    out = []
    all_categories = set(tops.keys()) | {c for c, _ in groups.keys()}
    for idx in sorted(all_categories):
        top = tops.get(idx, {})
        m1 = groups.get((idx, 1))
        out.append({
            "searchIndex": idx,
            "displayName": top.get("displayName") or idx,
            "method1": {"available": bool(m1), "nodeCount": (m1 or {}).get("node_count", 0),
                       "enabled": bool(m1 and m1["enabled_count"] == m1["node_count"] and m1["node_count"] > 0)},
        })
    return jsonify(out)


@app.route("/api/method_test/items")
def api_method_test_items():
    """Return the live category scanner feed."""
    method1 = get_products_for_user(METHOD_FEED_USER_IDS[1])
    return jsonify({"method1": method1})


@app.route("/api/method_test/toggle", methods=["POST"])
def api_method_test_toggle():
    """Switch a (category, method) on/off in the round robin."""
    data = request.json or {}
    category = str(data.get("category") or "").strip()
    method = data.get("method")
    enabled = bool(data.get("enabled"))
    marketplace = "DE"
    if not category or method != 1:
        return jsonify({"success": False, "error": "category and method 1 required"}), 400
    set_method_nodes_enabled(marketplace, category, int(method), enabled)
    return jsonify({"success": True})


@app.route("/api/method_test/control", methods=["POST"])
def api_method_test_control():
    action = (request.json or {}).get("action", "")
    try:
        if action == "start" or action == "resume":
            state = get_method_engine_state()
            # Stamp the uptime timer only on the OFF -> ON transition, so a
            # redundant 'resume' while already running doesn't reset it.
            if state.get("enabled"):
                update_method_engine_state(enabled=1)
            else:
                update_method_engine_state(
                    enabled=1,
                    started_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                )
        elif action in ("pause", "stop"):
            update_method_engine_state(enabled=0, running=0, started_at=None,
                                       current_target="Paused")
        else:
            return jsonify({"success": False, "error": "action must be 'start'/'resume' or 'pause'/'stop'"}), 400
        return jsonify({"success": True, "state": get_method_engine_state()})
    except Exception as e:
        logger.exception(f"[APP] Method engine control failed ({action}): {e}")
        return jsonify({
            "success": False,
            "error": "Engine control failed. Check the server log for details.",
        }), 500


@app.route("/api/method_test/status")
def api_method_test_status():
    mt_cfg = config.get("method_test", {}) or {}
    daily_budget = int(mt_cfg.get("daily_budget_requests", 8640))

    stats_rows = get_method_stats()
    stats_by_key = {
        (r["category"], int(r["method"]), r["marketplace"]): r
        for r in stats_rows
    }
    calls_today = sum(r.get("creators_api_calls") or 0 for r in stats_rows)

    nodes = [
        n for n in get_method_nodes(marketplace="DE")
        if int(n["method"]) == 1
    ]
    groups = {}
    for n in nodes:
        # marketplace is part of the key: nodes now exist per-marketplace, so
        # without it DE/GB/FR/ES rows for the same category+method would get
        # silently summed together into one row (and "marketplace" below would
        # just show whichever one happened to be processed last).
        key = (n["category"], int(n["method"]), n["marketplace"])
        g = groups.setdefault(key, {
            "category": n["category"], "method": int(n["method"]),
            "marketplace": n["marketplace"], "node_count": 0, "enabled_count": 0,
            "price_cursors": [],
        })
        g["node_count"] += 1
        if n.get("enabled"):
            g["enabled_count"] += 1
            g["price_cursors"].append(n.get("current_min_price") or 0)

    targets = []
    for key, g in sorted(groups.items()):
        s = stats_by_key.get(key, {})
        scanned = s.get("asins_scanned") or 0
        posted = s.get("posted") or 0
        targets.append({
            "category": g["category"],
            "method": g["method"],
            "marketplace": g["marketplace"],
            "enabled": g["enabled_count"] > 0,
            "node_count": g["node_count"],
            "enabled_node_count": g["enabled_count"],
            "avg_price_floor": round(sum(g["price_cursors"]) / len(g["price_cursors"]), 2) if g["price_cursors"] else 0,
            "creators_api_calls": s.get("creators_api_calls") or 0,
            "keepa_calls": s.get("keepa_calls") or 0,
            "asins_scanned": scanned,
            "cache_skipped": s.get("cache_skipped") or 0,
            "keepa_rejected": s.get("keepa_rejected") or 0,
            "ai_rejected": s.get("ai_rejected") or 0,
            "posted": posted,
            "success_rate": round(posted / scanned * 100, 1) if scanned else 0.0,
        })

    n_enabled_groups = sum(1 for g in groups.values() if g["enabled_count"] > 0)
    theoretical_share_today = round(daily_budget / n_enabled_groups, 1) if n_enabled_groups else 0

    engine = get_method_engine_state()
    engine["uptime_seconds"] = None
    if engine.get("enabled") and engine.get("started_at"):
        try:
            started = datetime.fromisoformat(str(engine["started_at"]))
            engine["uptime_seconds"] = max(0, int((datetime.utcnow() - started).total_seconds()))
        except (ValueError, TypeError):
            pass

    return jsonify({
        "engine": engine,
        "budget": {
            "daily_budget_requests": daily_budget,
            "asins_per_call": 10,
            "calls_today": calls_today,
            "remaining_today": max(0, daily_budget - calls_today),
            "pct_used": round(calls_today / daily_budget * 100, 1) if daily_budget else 0,
            "theoretical_share_per_active_target": theoretical_share_today,
        },
        "targets": targets,
    })


# ==================== Admin endpoints ====================

@app.route("/api/config", methods=["GET"])
def api_config():
    """Read-only view of server config (admin use)."""
    return jsonify(config)


@app.route("/api/reinit_db", methods=["POST"])
def api_reinit_db():
    try:
        reset_db()
        return jsonify({"success": True, "message": "Database reinitialized"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ==================== Main ====================

if __name__ == "__main__":
    try:
        init_db()
        logger.info("[APP] Database initialized")
    except Exception as e:
        logger.error(f"[APP] Database init error: {e}")

    logger.info("[APP] Starting Creators Deal Finder")
    port = int(os.environ.get("PORT", 8888))
    app.run(host="0.0.0.0", port=port, debug=False)
