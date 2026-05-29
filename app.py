import yaml
import logging
import os

from flask import Flask, render_template, jsonify, request

from scheduler import DealScheduler
from testing_stats import TestingStats
from database import (
    init_db, reset_db,
    get_products_for_user, get_stats_for_user, clear_products_for_user,
    get_user_preferences, save_user_preferences,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')


def _upper_key(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(s)).upper()


def _env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return None


def _apply_env_overrides(cfg: dict) -> dict:
    cfg = cfg or {}

    wh = _env("DISCORD_WEBHOOK", "DISCORD_WEBHOOK_URL")
    if wh:
        cfg.setdefault("discord", {})["webhook_url"] = wh

    keepa_key = _env("KEEPA_API_KEY")
    if keepa_key:
        cfg.setdefault("keepa", {})["api_key"] = keepa_key
    keepa_enabled = _env("KEEPA_ENABLED")
    if keepa_enabled is not None:
        try:
            cfg.setdefault("keepa", {})["enabled"] = str(keepa_enabled).lower() in ("1", "true", "yes", "on")
        except Exception:
            pass

    apify = _env("APIFY_API_TOKEN")
    if apify:
        cfg["apify_api_token"] = apify

    for k, v in list(cfg.items()):
        try:
            if not (isinstance(k, str) and k.startswith("Amazon_")):
                continue
            section = cfg.get(k) or {}
            up = _upper_key(k)
            for field in ("Application", "Application_Id", "Credential_Id", "Secret"):
                env_name = f"{up}_{_upper_key(field)}"
                ev = _env(env_name)
                if ev:
                    section[field] = ev
            cfg[k] = section
        except Exception:
            continue

    return cfg


# Load server config (admin-only, used as defaults for AI/Keepa/Discord)
try:
    with open("config.yml", "r") as f:
        config = yaml.safe_load(f) or {}
    config = _apply_env_overrides(config)
    logger.info("[APP] Config loaded from config.yml")
except Exception as e:
    logger.error(f"[APP] Failed to load config: {e}")
    config = _apply_env_overrides({})


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
    raw_mks = data.get("marketplaces", ["DE"])
    marketplaces = [str(m).upper() for m in (raw_mks if isinstance(raw_mks, list) else [raw_mks])] or ["DE"]
    pages = max(1, min(int(data.get("pages", 1)), 20))
    min_saving = int(data.get("min_saving", config.get("amazon", {}).get("min_saving_percent", 50)))
    max_price = float(data.get("max_price", config.get("amazon", {}).get("max_price", 450)))

    # Build per-request config inheriting server defaults
    search_config = dict(config)
    search_config["amazon"] = dict(config.get("amazon", {}))
    search_config["amazon"]["keywords"] = keywords
    search_config["amazon"]["min_saving_percent"] = min_saving
    search_config["amazon"]["max_price"] = max_price

    try:
        stats = TestingStats()
        scheduler = DealScheduler(search_config, stats)
        results = scheduler.search_for_user(
            user_id=user_id,
            keywords=keywords,
            marketplaces=marketplaces,
            pages=pages,
            min_saving=min_saving,
            max_price=max_price,
        )
        return jsonify({
            "success": True,
            "found": len(results),
            "api_calls": stats.api_calls,
            "results": results,
        })
    except Exception as e:
        logger.exception(f"[APP] Search error for user {user_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== Test endpoint (unchanged) ====================

@app.route("/api/test", methods=["POST"])
def api_test():
    import time
    from creators_search import CreatorsSearch
    from keepa_service import KeepaService
    from ai_scoring import AIScorer

    try:
        params = request.json
        test_config = dict(config)
        test_config["amazon"]["marketplace"] = params.get("marketplace", "GB")
        test_config["amazon"]["min_saving_percent"] = params.get("min_saving", 50)
        test_config["amazon"]["max_price"] = params.get("max_price", 450)
        test_config["amazon"]["keywords"] = params.get("keywords", test_config.get("amazon", {}).get("keywords", ""))
        test_config["filters"]["min_keepa_drop_percent"] = params.get("min_drop", 35)
        test_config["filters"]["min_rating"] = params.get("min_rating", 4.0)
        test_config["filters"]["min_review_count"] = params.get("min_reviews", 10)
        test_config["ai"]["enabled"] = params.get("use_ai", True)
        test_config["ai"]["minimum_score"] = params.get("min_ai_score", 7)
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

                    ai_score = 5.0
                    if test_config["ai"]["enabled"] and keepa_passed_item and title:
                        try:
                            ai_score = ai.score_deal(title, asin)
                            if ai_score >= test_config["ai"]["minimum_score"]:
                                ai_passed += 1
                        except Exception as e:
                            logger.warning(f"[TEST] AI error for {asin}: {e}")

                    results.append({
                        "asin": asin,
                        "title": title[:80],
                        "price": float(price_amt) if price_amt is not None else None,
                        "keepa_drop": keepa_drop,
                        "ai_score": ai_score,
                        "page": page_num,
                        "url": f"https://www.amazon.{creators._tld_for_marketplace(test_config['amazon']['marketplace'])}/dp/{asin}",
                    })
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
