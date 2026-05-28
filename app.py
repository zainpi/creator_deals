import asyncio
import threading
import yaml
import logging
import os

from flask import Flask, render_template, jsonify, request

from scheduler import DealScheduler
from testing_stats import TestingStats
from database import init_db, get_all_products, get_stats, clear_all_products, reset_db

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')

# Load config
try:
    with open("config.yml", "r") as f:
        config = yaml.safe_load(f)
    logger.info("[APP] Config loaded from config.yml")
except Exception as e:
    logger.error(f"[APP] Failed to load config: {e}")
    config = {}

# Initialize components
stats = TestingStats()
scheduler = DealScheduler(config, stats)
scanner_thread = None
scanner_loop = None


# ==================== API Endpoints ====================

@app.route("/")
def index():
    """Dashboard homepage."""
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    """Get discovery statistics."""
    db_stats = get_stats()
    api_stats = stats.as_dict()
    api_stats.update(db_stats)
    return jsonify(api_stats)


@app.route("/api/products")
def api_products():
    """Get recent discovered products."""
    products = get_all_products()
    return jsonify(products)


@app.route("/api/start", methods=["POST"])
def api_start():
    """Start the scanner."""
    global scanner_thread, scanner_loop

    if scanner_thread and scanner_thread.is_alive():
        return jsonify({"success": False, "message": "Scanner already running"}), 400

    def run_loop():
        global scanner_loop
        scanner_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(scanner_loop)
        try:
            scanner_loop.run_until_complete(scheduler.scan_loop())
        except Exception as e:
            logger.error(f"[APP] Scanner loop error: {e}")
        finally:
            scanner_loop.close()

    scanner_thread = threading.Thread(target=run_loop, daemon=True)
    scanner_thread.start()

    logger.info("[APP] Scanner started")
    return jsonify({"success": True, "message": "Scanner started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Stop the scanner."""
    scheduler.stop()
    logger.info("[APP] Scanner stop requested")
    return jsonify({"success": True, "message": "Scanner stopped"})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Get or update configuration."""
    global config, scheduler
    
    if request.method == "GET":
        return jsonify(config)
    
    # POST: Update config (deep merge)
    try:
        updates = request.json
        
        def deep_merge(target, source):
            """Recursively merge source into target."""
            for key, value in source.items():
                if isinstance(value, dict) and key in target and isinstance(target[key], dict):
                    deep_merge(target[key], value)
                else:
                    target[key] = value
        
        deep_merge(config, updates)
        
        # Save to file
        with open("config.yml", "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        
        # Reload blocked categories if filters were updated
        if "filters" in updates:
            from filters import set_blocked_categories
            set_blocked_categories(config)
        
        # Propagate updates to running components
        try:
            if 'scheduler' in globals() and scheduler is not None:
                scheduler.config = config
                # Update searcher config reference and reload credentials
                try:
                    scheduler.searcher.config = config
                    if hasattr(scheduler.searcher, '_load_credentials'):
                        scheduler.searcher._load_credentials()
                except Exception:
                    pass
                # Update keepa instance (enable/disable based on config every save)
                try:
                    keepa_cfg = config.get('keepa', {}) or {}
                    keepa_enabled = keepa_cfg.get('enabled', True)
                    keepa_key = keepa_cfg.get('api_key') or keepa_cfg.get('key') or keepa_cfg.get('apiKey')
                    from keepa_service import KeepaService
                    if keepa_enabled and keepa_key:
                        # Always reinitialize to pick up new keys/settings
                        scheduler.keepa = KeepaService(keepa_key)
                    else:
                        # Disable Keepa when not enabled or missing key
                        scheduler.keepa = None
                except Exception:
                    pass

        except Exception:
            logger.exception("[APP] Failed to propagate config to running components")

        logger.info("[APP] Config updated")
        return jsonify({"success": True, "message": "Config updated and saved"})
        
    except Exception as e:
        logger.error(f"[APP] Config update error: {e}")
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset statistics."""
    global stats
    stats = TestingStats()
    logger.info("[APP] Stats reset")
    return jsonify({"success": True, "message": "Stats reset"})


@app.route("/api/clear_products", methods=["POST"])
def api_clear_products():
    """Clear all discovered products from the database."""
    try:
        clear_all_products()
        logger.info("[APP] Cleared all discovered products")
        return jsonify({"success": True, "message": "All discoveries cleared"})
    except Exception as e:
        logger.error(f"[APP] Clear products error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reinit_db", methods=["POST"])
def api_reinit_db():
    """Drop and recreate the discoveries table (schema reset)."""
    try:
        reset_db()
        logger.info("[APP] Database schema reinitialized")
        return jsonify({"success": True, "message": "Database reinitialized"})
    except Exception as e:
        logger.error(f"[APP] Reinit DB error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/test", methods=["POST"])
def api_test():
    """Test scan multiple pages with custom filters."""
    import time
    from creators_search import CreatorsSearch
    from keepa_service import KeepaService
    from ai_scoring import AIScorer
    
    try:
        params = request.json
        
        # Build test config
        test_config = dict(config)
        test_config["amazon"]["marketplace"] = params.get("marketplace", "GB")
        test_config["amazon"]["min_saving_percent"] = params.get("min_saving", 50)
        test_config["amazon"]["max_price"] = params.get("max_price", 450)
        # Keywords for search (optional)
        test_config["amazon"]["keywords"] = params.get("keywords", test_config.get("amazon", {}).get("keywords", ""))
        test_config["filters"]["min_keepa_drop_percent"] = params.get("min_drop", 35)
        test_config["filters"]["min_rating"] = params.get("min_rating", 4.0)
        test_config["filters"]["min_review_count"] = params.get("min_reviews", 10)
        test_config["ai"]["enabled"] = params.get("use_ai", True)
        test_config["ai"]["minimum_score"] = params.get("min_ai_score", 7)
        # Keepa toggle for test; default True if not provided
        if "keepa" not in test_config:
            test_config["keepa"] = {}
        test_config["keepa"]["enabled"] = params.get("use_keepa", test_config.get("keepa", {}).get("enabled", True))
        
        # Initialize services
        creators = CreatorsSearch(test_config)
        keepa = KeepaService(test_config) if test_config.get("keepa", {}).get("enabled", True) else None
        ai = AIScorer(test_config)
        
        # Pagination
        start_page = params.get("page_start", 1)
        end_page = params.get("page_end", 1)
        
        start_time = time.time()
        
        found_count = 0
        keepa_passed = 0
        ai_passed = 0
        results = []
        errors = []
        
        # Scan multiple pages
        for page_num in range(start_page, end_page + 1):
            try:
                # Search items
                items = creators.search_items(
                    page=page_num,
                    min_saving_percent=params.get("min_saving", 50),
                    max_price=params.get("max_price", 450)
                )
                
                found_count += len(items)
                
                # Filter items
                for item in items:
                    asin = item.get("ASIN", "")
                    
                    # Extract minimal data
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
            "results": results[:50]  # Return top 50
        })
        
    except Exception as e:
        logger.error(f"[APP] Test scan error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health")
def health():
    """Health check."""
    return jsonify({"status": "ok", "running": scheduler.running})


# ==================== Main ====================

if __name__ == "__main__":
    try:
        init_db()
        logger.info("[APP] Database initialized")
    except Exception as e:
        logger.error(f"[APP] Database init error: {e}")

    logger.info("[APP] Starting Creators Deal Finder")
    app.run(
        host="0.0.0.0",
        port=8888,
        debug=False
    )
