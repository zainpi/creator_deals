"""Data layer for the Creators Deal Finder.

Works on two backends, chosen automatically:
- **Postgres** when `DATABASE_URL` is set (Heroku) — shared & persistent across
  the web and worker dynos.
- **SQLite** otherwise (local dev / tests) — file at DB_PATH.

SQL is written once with `?` placeholders and upserts use `ON CONFLICT`, which
both modern SQLite (3.24+) and Postgres support. `_q()` swaps `?` -> `%s` for
psycopg2; `_connect()` returns the right connection.
"""

import os
import sqlite3
from datetime import datetime, timedelta

try:
    import psycopg2
except ImportError:
    psycopg2 = None

DATABASE_URL = os.getenv("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL) and psycopg2 is not None

DB_PATH = "data/discovered_asins.db"

# Reserved user_id for the shared, continuously-scanned discovery feed.
GLOBAL_FEED_USER_ID = "__global__"

# Reserved user_ids for the Method 1 / Method 2 comparison engine's result
# feeds (method_scanner.py) — lets the existing discovered_products table /
# get_products_for_user() double as a viewable history of what each method
# posted, without touching per-user search data.
METHOD_FEED_USER_IDS = {1: "__method1__", 2: "__method2__"}


def _connect():
    if IS_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, sslmode=os.getenv("PGSSLMODE", "require"))
        conn.autocommit = True  # explicit commit() calls below become harmless no-ops
        return conn
    # SQLite: ensure the parent directory exists (the DB file is gitignored)
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=30)


def _q(sql: str) -> str:
    """Translate `?` placeholders to `%s` for psycopg2."""
    return sql.replace("?", "%s") if IS_POSTGRES else sql


def _migrate(conn):
    """SQLite-only schema backfill for pre-existing local databases.
    Postgres deployments start fresh with the full schema below, so no migration."""
    if IS_POSTGRES:
        return
    c = conn.cursor()

    # ---- discovered_products ----
    c.execute("PRAGMA table_info('discovered_products')")
    cols = [row[1] for row in c.fetchall()]
    if cols:
        if 'user_id' not in cols:
            # Old schema without user_id — must drop and recreate (can't change PK)
            c.execute("DROP TABLE discovered_products")
            conn.commit()
        elif 'seller_rating' not in cols:
            c.execute("ALTER TABLE discovered_products ADD COLUMN seller_rating REAL")
            conn.commit()

    # ---- user_preferences: backfill any columns added after the table first shipped ----
    c.execute("PRAGMA table_info('user_preferences')")
    pref_cols = [row[1] for row in c.fetchall()]
    if pref_cols:
        for col, ddl in (
            ("sort_by", "TEXT"),
            ("use_filters", "INTEGER DEFAULT 1"),
            ("use_keepa", "INTEGER DEFAULT 1"),
            ("use_ai", "INTEGER DEFAULT 1"),
            ("f_min_saving", "INTEGER DEFAULT 0"),
            ("f_min_ai_score", "REAL DEFAULT 0"),
            ("f_min_seller_rating", "REAL DEFAULT 0"),
            ("f_min_price", "REAL DEFAULT 0"),
            ("f_max_price", "REAL DEFAULT 0"),
        ):
            if col not in pref_cols:
                c.execute(f"ALTER TABLE user_preferences ADD COLUMN {col} {ddl}")
        conn.commit()


def init_db():
    conn = _connect()
    _migrate(conn)
    c = conn.cursor()

    # Per-user discoveries — PRIMARY KEY is (user_id, asin)
    c.execute('''
        CREATE TABLE IF NOT EXISTS discovered_products (
            user_id TEXT NOT NULL,
            asin TEXT NOT NULL,
            title TEXT,
            marketplace TEXT,
            current_price REAL,
            savings_percent REAL,
            category TEXT,
            seller_name TEXT,
            seller_id TEXT,
            image TEXT,
            keepa_avg_90 REAL,
            keepa_drop_percent REAL,
            ai_score REAL,
            ai_reason TEXT,
            page_found INTEGER,
            seller_rating REAL,
            first_seen TEXT,
            last_seen TEXT,
            posted INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, asin)
        )
    ''')

    # Per-user search preferences
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id TEXT PRIMARY KEY,
            keywords TEXT,
            marketplaces TEXT,
            min_saving INTEGER,
            max_price REAL,
            pages INTEGER,
            sort_by TEXT,
            use_filters INTEGER DEFAULT 1,
            use_keepa INTEGER DEFAULT 1,
            use_ai INTEGER DEFAULT 1,
            f_min_saving INTEGER DEFAULT 0,
            f_min_ai_score REAL DEFAULT 0,
            f_min_seller_rating REAL DEFAULT 0,
            f_min_price REAL DEFAULT 0,
            f_max_price REAL DEFAULT 0,
            updated_at TEXT
        )
    ''')

    # Background search job status (one row per user; shared across workers)
    c.execute('''
        CREATE TABLE IF NOT EXISTS search_jobs (
            user_id TEXT PRIMARY KEY,
            status TEXT,
            found INTEGER DEFAULT 0,
            api_calls INTEGER DEFAULT 0,
            error TEXT,
            started_at TEXT,
            finished_at TEXT
        )
    ''')

    # Background batch-diagnostic job (single row, id=1; shared across workers).
    # `result` holds the full JSON payload the frontend renders once status='done'.
    c.execute('''
        CREATE TABLE IF NOT EXISTS batch_jobs (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            status TEXT,
            total INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            error TEXT,
            result TEXT,
            started_at TEXT,
            finished_at TEXT
        )
    ''')
    c.execute("INSERT INTO batch_jobs (id) VALUES (1) ON CONFLICT DO NOTHING")

    # Continuous scanner state (single row, id=1). The web process reads this;
    # the worker process writes it.
    c.execute('''
        CREATE TABLE IF NOT EXISTS scanner_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            tick INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            running INTEGER DEFAULT 0,
            last_heartbeat TEXT,
            current_target TEXT,
            scanned_count INTEGER DEFAULT 0,
            kept_count INTEGER DEFAULT 0,
            last_error TEXT
        )
    ''')
    c.execute("INSERT INTO scanner_state (id) VALUES (1) ON CONFLICT DO NOTHING")

    # Learned "typical price" per (marketplace, category), built from Keepa 90d avgs.
    c.execute('''
        CREATE TABLE IF NOT EXISTS category_baselines (
            marketplace TEXT NOT NULL,
            category TEXT NOT NULL,
            sample_count INTEGER DEFAULT 0,
            avg90_sum REAL DEFAULT 0,
            avg90_mean REAL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (marketplace, category)
        )
    ''')

    # Cooldown ledger: every ASIN we evaluate, to avoid re-spending Keepa too soon.
    c.execute('''
        CREATE TABLE IF NOT EXISTS scan_seen (
            marketplace TEXT NOT NULL,
            asin TEXT NOT NULL,
            last_scanned_at TEXT,
            PRIMARY KEY (marketplace, asin)
        )
    ''')

    # ============== Method 1 vs Method 2 comparison engine (method_scanner.py) ==============

    # Flattened round-robin universe: one row per browse node the engine can scan.
    # Method 1 categories seed one row per subcategory (data/categories.json);
    # Method 2 categories seed a single row using the parent browse node
    # (data/topcategories.json). current_min_price is that node's price-sweep
    # cursor (persists across restarts/ticks); enabled is the user's on/off toggle
    # from the "Pick a Category" UI.
    c.execute('''
        CREATE TABLE IF NOT EXISTS method_nodes (
            marketplace TEXT NOT NULL,
            category TEXT NOT NULL,
            method INTEGER NOT NULL,
            browse_node_id TEXT NOT NULL,
            label TEXT,
            keywords TEXT,
            enabled INTEGER DEFAULT 1,
            current_min_price REAL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (marketplace, category, method, browse_node_id)
        )
    ''')

    # Price-aware ASIN cache: "if ASIN in cache and price unchanged, skip" —
    # checked BEFORE spending a Keepa call. Separate from scan_seen (which is a
    # time-only cooldown for the older continuous_scanner engine).
    c.execute('''
        CREATE TABLE IF NOT EXISTS method_asin_cache (
            marketplace TEXT NOT NULL,
            method INTEGER NOT NULL,
            asin TEXT NOT NULL,
            last_price REAL,
            last_checked_at TEXT,
            PRIMARY KEY (marketplace, method, asin)
        )
    ''')

    # Per-day, per-category/method credit + outcome counters — powers the
    # "Theoretical vs Actual credit consumption" and Keepa success-rate views.
    c.execute('''
        CREATE TABLE IF NOT EXISTS method_stats (
            marketplace TEXT NOT NULL,
            category TEXT NOT NULL,
            method INTEGER NOT NULL,
            date TEXT NOT NULL,
            creators_api_calls INTEGER DEFAULT 0,
            keepa_calls INTEGER DEFAULT 0,
            asins_scanned INTEGER DEFAULT 0,
            cache_skipped INTEGER DEFAULT 0,
            keepa_rejected INTEGER DEFAULT 0,
            ai_rejected INTEGER DEFAULT 0,
            posted INTEGER DEFAULT 0,
            PRIMARY KEY (marketplace, category, method, date)
        )
    ''')

    # Single-row engine on/off + round-robin pointer + heartbeat (mirrors scanner_state).
    c.execute('''
        CREATE TABLE IF NOT EXISTS method_engine_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER DEFAULT 0,
            running INTEGER DEFAULT 0,
            rr_pointer INTEGER DEFAULT 0,
            last_heartbeat TEXT,
            current_target TEXT,
            last_error TEXT,
            started_at TEXT
        )
    ''')
    c.execute("INSERT INTO method_engine_state (id) VALUES (1) ON CONFLICT DO NOTHING")

    # Migration: add started_at (engine uptime timer) to pre-existing DBs.
    c.execute("PRAGMA table_info('method_engine_state')")
    if "started_at" not in {row[1] for row in c.fetchall()}:
        c.execute("ALTER TABLE method_engine_state ADD COLUMN started_at TEXT")

    conn.commit()
    conn.close()


def asin_exists(user_id, asin):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        _q("SELECT asin FROM discovered_products WHERE user_id=? AND asin=?"),
        (user_id, asin),
    )
    result = c.fetchone()
    conn.close()
    return result is not None


def insert_product(product):
    conn = _connect()
    c = conn.cursor()
    c.execute(_q('''
        INSERT INTO discovered_products (
            user_id, asin, title, marketplace, current_price, savings_percent,
            category, seller_name, seller_id, image, keepa_avg_90, keepa_drop_percent,
            ai_score, ai_reason, page_found, seller_rating, first_seen, last_seen, posted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, asin) DO UPDATE SET
            title=EXCLUDED.title,
            marketplace=EXCLUDED.marketplace,
            current_price=EXCLUDED.current_price,
            savings_percent=EXCLUDED.savings_percent,
            category=EXCLUDED.category,
            seller_name=EXCLUDED.seller_name,
            seller_id=EXCLUDED.seller_id,
            image=EXCLUDED.image,
            keepa_avg_90=EXCLUDED.keepa_avg_90,
            keepa_drop_percent=EXCLUDED.keepa_drop_percent,
            ai_score=EXCLUDED.ai_score,
            ai_reason=EXCLUDED.ai_reason,
            page_found=EXCLUDED.page_found,
            seller_rating=EXCLUDED.seller_rating,
            last_seen=EXCLUDED.last_seen,
            posted=EXCLUDED.posted
    '''), (
        product["user_id"],
        product["asin"],
        product.get("title"),
        product.get("marketplace"),
        product.get("current_price"),
        product.get("savings_percent"),
        product.get("category"),
        product.get("seller_name"),
        product.get("seller_id"),
        product.get("image"),
        product.get("keepa_avg_90"),
        product.get("keepa_drop_percent"),
        product.get("ai_score"),
        product.get("ai_reason"),
        product.get("page_found"),
        product.get("seller_rating"),
        product.get("first_seen", datetime.utcnow().isoformat()),
        datetime.utcnow().isoformat(),
        int(product.get("posted", False)),
    ))
    conn.commit()
    conn.close()


def get_products_for_user(user_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        _q("SELECT * FROM discovered_products WHERE user_id=? ORDER BY last_seen DESC"),
        (user_id,),
    )
    columns = [desc[0] for desc in c.description]
    rows = c.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]


def get_stats_for_user(user_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(_q("SELECT COUNT(*) FROM discovered_products WHERE user_id=?"), (user_id,))
    total = c.fetchone()[0]
    c.execute(_q("SELECT COUNT(*) FROM discovered_products WHERE user_id=? AND posted=1"), (user_id,))
    posted = c.fetchone()[0]
    conn.close()
    return {"total_discovered": total, "total_posted": posted}


def clear_products_for_user(user_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(_q("DELETE FROM discovered_products WHERE user_id=?"), (user_id,))
    conn.commit()
    conn.close()


def get_user_preferences(user_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(_q("SELECT * FROM user_preferences WHERE user_id=?"), (user_id,))
    row = c.fetchone()
    # Use the cursor's actual column names so order always matches the live schema
    columns = [d[0] for d in c.description]
    conn.close()
    if not row:
        return {}
    return dict(zip(columns, row))


def save_user_preferences(user_id, prefs):
    conn = _connect()
    c = conn.cursor()
    c.execute(_q('''
        INSERT INTO user_preferences
            (user_id, keywords, marketplaces, min_saving, max_price, pages, sort_by,
             use_filters, use_keepa, use_ai, f_min_saving, f_min_ai_score,
             f_min_seller_rating, f_min_price, f_max_price, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            keywords=EXCLUDED.keywords,
            marketplaces=EXCLUDED.marketplaces,
            min_saving=EXCLUDED.min_saving,
            max_price=EXCLUDED.max_price,
            pages=EXCLUDED.pages,
            sort_by=EXCLUDED.sort_by,
            use_filters=EXCLUDED.use_filters,
            use_keepa=EXCLUDED.use_keepa,
            use_ai=EXCLUDED.use_ai,
            f_min_saving=EXCLUDED.f_min_saving,
            f_min_ai_score=EXCLUDED.f_min_ai_score,
            f_min_seller_rating=EXCLUDED.f_min_seller_rating,
            f_min_price=EXCLUDED.f_min_price,
            f_max_price=EXCLUDED.f_max_price,
            updated_at=EXCLUDED.updated_at
    '''), (
        user_id,
        prefs.get("keywords", ""),
        prefs.get("marketplaces", "DE"),
        prefs.get("min_saving", 50),
        prefs.get("max_price", 450),
        prefs.get("pages", 1),
        prefs.get("sort_by", "Featured"),
        int(prefs.get("use_filters", True)),
        int(prefs.get("use_keepa", True)),
        int(prefs.get("use_ai", True)),
        prefs.get("f_min_saving", 0),
        prefs.get("f_min_ai_score", 0),
        prefs.get("f_min_seller_rating", 0),
        prefs.get("f_min_price", 0),
        prefs.get("f_max_price", 0),
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()


def set_search_job(user_id, **fields):
    """Upsert a user's background-search job status. Only updates given fields.

    Field names are controlled by callers (status, found, api_calls, error,
    started_at, finished_at) — not user input — so interpolation is safe here.
    """
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(_q("INSERT INTO search_jobs (user_id) VALUES (?) ON CONFLICT DO NOTHING"), (user_id,))
        if fields:
            assignments = ", ".join(f"{k}=?" for k in fields)
            c.execute(
                _q(f"UPDATE search_jobs SET {assignments} WHERE user_id=?"),
                (*fields.values(), user_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_search_job(user_id):
    conn = _connect()
    c = conn.cursor()
    c.execute(_q("SELECT * FROM search_jobs WHERE user_id=?"), (user_id,))
    row = c.fetchone()
    columns = [d[0] for d in c.description]
    conn.close()
    if not row:
        return {}
    return dict(zip(columns, row))


# ==================== Background batch-diagnostic job ====================

def set_batch_job(**fields):
    """Update the single batch-job row (id=1). Only updates given fields.

    Field names are caller-controlled (status, total, completed, error, result,
    started_at, finished_at) — not user input — so interpolation is safe here.
    """
    if not fields:
        return
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO batch_jobs (id) VALUES (1) ON CONFLICT DO NOTHING")
        assignments = ", ".join(f"{k}=?" for k in fields)
        c.execute(_q(f"UPDATE batch_jobs SET {assignments} WHERE id=1"),
                  tuple(fields.values()))
        conn.commit()
    finally:
        conn.close()


def get_batch_job():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM batch_jobs WHERE id=1")
    row = c.fetchone()
    columns = [d[0] for d in c.description]
    conn.close()
    if not row:
        return {}
    return dict(zip(columns, row))


# ==================== Continuous scanner state ====================

def get_scanner_state():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM scanner_state WHERE id=1")
    row = c.fetchone()
    columns = [d[0] for d in c.description]
    conn.close()
    if not row:
        return {}
    return dict(zip(columns, row))


def update_scanner_state(**fields):
    """Update the single scanner_state row. Field names are caller-controlled
    (tick, enabled, running, last_heartbeat, current_target, scanned_count,
    kept_count, last_error) — not user input — so interpolation is safe."""
    if not fields:
        return
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO scanner_state (id) VALUES (1) ON CONFLICT DO NOTHING")
        assignments = ", ".join(f"{k}=?" for k in fields)
        c.execute(_q(f"UPDATE scanner_state SET {assignments} WHERE id=1"),
                  tuple(fields.values()))
        conn.commit()
    finally:
        conn.close()


# ==================== Category baselines (learned typical price) ====================

def get_category_baselines(marketplace):
    """Return {category: {sample_count, avg90_mean, ...}} for a marketplace."""
    conn = _connect()
    c = conn.cursor()
    c.execute(_q("SELECT * FROM category_baselines WHERE marketplace=?"), (marketplace,))
    columns = [d[0] for d in c.description]
    rows = c.fetchall()
    conn.close()
    return {row[columns.index("category")]: dict(zip(columns, row)) for row in rows}


def bump_category_baseline(marketplace, category, avg90):
    """Fold one Keepa 90-day average into a category's running mean."""
    if not category or avg90 is None:
        return
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(_q('''
            INSERT INTO category_baselines (marketplace, category, sample_count, avg90_sum, avg90_mean, updated_at)
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT (marketplace, category) DO UPDATE SET
                sample_count = category_baselines.sample_count + 1,
                avg90_sum    = category_baselines.avg90_sum + EXCLUDED.avg90_sum,
                avg90_mean   = (category_baselines.avg90_sum + EXCLUDED.avg90_sum) / (category_baselines.sample_count + 1.0),
                updated_at   = EXCLUDED.updated_at
        '''), (marketplace, category, float(avg90), float(avg90), datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()


# ==================== Scan cooldown ledger ====================

def seen_recently(marketplace, asin, cooldown_hours):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        _q("SELECT last_scanned_at FROM scan_seen WHERE marketplace=? AND asin=?"),
        (marketplace, asin),
    )
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return False
    try:
        last = datetime.fromisoformat(row[0])
    except Exception:
        return False
    return datetime.utcnow() - last < timedelta(hours=cooldown_hours)


def mark_seen(marketplace, asin):
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(_q('''
            INSERT INTO scan_seen (marketplace, asin, last_scanned_at)
            VALUES (?, ?, ?)
            ON CONFLICT (marketplace, asin) DO UPDATE SET
                last_scanned_at=EXCLUDED.last_scanned_at
        '''), (marketplace, asin, datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()


# ==================== Method comparison engine (method_scanner.py) ====================

def seed_method_node(marketplace, category, method, browse_node_id, label=None, keywords=None, enabled=1):
    """Insert a round-robin node if it doesn't exist yet. Idempotent — re-seeding
    (e.g. on every app start) refreshes label/keywords but never resets an
    existing node's enabled flag or its in-progress price cursor."""
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(_q('''
            INSERT INTO method_nodes
                (marketplace, category, method, browse_node_id, label, keywords, enabled, current_min_price, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT (marketplace, category, method, browse_node_id) DO UPDATE SET
                label=EXCLUDED.label,
                keywords=EXCLUDED.keywords
        '''), (marketplace, category, int(method), str(browse_node_id), label, keywords,
              int(enabled), datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()


def get_method_nodes(marketplace=None, enabled_only=False):
    conn = _connect()
    c = conn.cursor()
    sql = "SELECT * FROM method_nodes"
    clauses, params = [], []
    if marketplace:
        clauses.append("marketplace=?")
        params.append(marketplace)
    if enabled_only:
        clauses.append("enabled=1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY category, method, browse_node_id"
    c.execute(_q(sql), tuple(params))
    columns = [d[0] for d in c.description]
    rows = c.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]


def set_method_nodes_enabled(marketplace, category, method, enabled):
    """Bulk-toggle every node for a (marketplace, category, method) combo — this is
    what the 'Method 1 / Method 2' switches in the UI flip per category."""
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(
            _q("UPDATE method_nodes SET enabled=?, updated_at=? WHERE marketplace=? AND category=? AND method=?"),
            (int(enabled), datetime.utcnow().isoformat(), marketplace, category, int(method)),
        )
        conn.commit()
    finally:
        conn.close()


def update_method_node_price(marketplace, category, method, browse_node_id, current_min_price):
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute(_q('''
            UPDATE method_nodes SET current_min_price=?, updated_at=?
            WHERE marketplace=? AND category=? AND method=? AND browse_node_id=?
        '''), (float(current_min_price), datetime.utcnow().isoformat(),
              marketplace, category, int(method), str(browse_node_id)))
        conn.commit()
    finally:
        conn.close()


def get_method_asin_cache_batch(marketplace, method, asins):
    """Return {asin: last_price} for the given ASINs (only those already cached)."""
    if not asins:
        return {}
    conn = _connect()
    c = conn.cursor()
    placeholders = ",".join("?" for _ in asins)
    c.execute(
        _q(f"SELECT asin, last_price FROM method_asin_cache "
           f"WHERE marketplace=? AND method=? AND asin IN ({placeholders})"),
        (marketplace, int(method), *asins),
    )
    rows = c.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def upsert_method_asin_cache_batch(marketplace, method, asin_price_pairs):
    """asin_price_pairs: iterable of (asin, price)."""
    pairs = [(a, p) for a, p in asin_price_pairs if a]
    if not pairs:
        return
    conn = _connect()
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    try:
        for asin, price in pairs:
            c.execute(_q('''
                INSERT INTO method_asin_cache (marketplace, method, asin, last_price, last_checked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (marketplace, method, asin) DO UPDATE SET
                    last_price=EXCLUDED.last_price,
                    last_checked_at=EXCLUDED.last_checked_at
            '''), (marketplace, int(method), asin, price, now))
        conn.commit()
    finally:
        conn.close()


def bump_method_stats(marketplace, category, method, date=None, **increments):
    """Add to today's counters for a (marketplace, category, method). `increments`
    keys must be columns on method_stats (creators_api_calls, keepa_calls,
    asins_scanned, cache_skipped, keepa_rejected, ai_rejected, posted)."""
    if not increments:
        return
    date = date or datetime.utcnow().strftime("%Y-%m-%d")
    conn = _connect()
    c = conn.cursor()
    try:
        cols = ", ".join(increments.keys())
        vals_ph = ", ".join("?" for _ in increments)
        updates = ", ".join(f"{k}=method_stats.{k}+EXCLUDED.{k}" for k in increments)
        c.execute(_q(f'''
            INSERT INTO method_stats (marketplace, category, method, date, {cols})
            VALUES (?, ?, ?, ?, {vals_ph})
            ON CONFLICT (marketplace, category, method, date) DO UPDATE SET
                {updates}
        '''), (marketplace, category, int(method), date, *increments.values()))
        conn.commit()
    finally:
        conn.close()


def get_method_stats(date=None):
    """Return today's (or `date`'s) per marketplace/category/method rows."""
    date = date or datetime.utcnow().strftime("%Y-%m-%d")
    conn = _connect()
    c = conn.cursor()
    c.execute(_q("SELECT * FROM method_stats WHERE date=?"), (date,))
    columns = [d[0] for d in c.description]
    rows = c.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]


def get_method_engine_state():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM method_engine_state WHERE id=1")
    row = c.fetchone()
    columns = [d[0] for d in c.description]
    conn.close()
    if not row:
        return {}
    return dict(zip(columns, row))


def update_method_engine_state(**fields):
    """Field names are caller-controlled (enabled, running, rr_pointer,
    last_heartbeat, current_target, last_error) — not user input."""
    if not fields:
        return
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO method_engine_state (id) VALUES (1) ON CONFLICT DO NOTHING")
        assignments = ", ".join(f"{k}=?" for k in fields)
        c.execute(_q(f"UPDATE method_engine_state SET {assignments} WHERE id=1"),
                  tuple(fields.values()))
        conn.commit()
    finally:
        conn.close()


def reset_db():
    """Drop and recreate all tables (schema reset)."""
    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("DROP TABLE IF EXISTS discovered_products")
        c.execute("DROP TABLE IF EXISTS user_preferences")
        c.execute("DROP TABLE IF EXISTS search_jobs")
        c.execute("DROP TABLE IF EXISTS batch_jobs")
        c.execute("DROP TABLE IF EXISTS scanner_state")
        c.execute("DROP TABLE IF EXISTS category_baselines")
        c.execute("DROP TABLE IF EXISTS scan_seen")
        c.execute("DROP TABLE IF EXISTS method_nodes")
        c.execute("DROP TABLE IF EXISTS method_asin_cache")
        c.execute("DROP TABLE IF EXISTS method_stats")
        c.execute("DROP TABLE IF EXISTS method_engine_state")
        conn.commit()
    finally:
        conn.close()
    init_db()
