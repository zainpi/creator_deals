import sqlite3
from datetime import datetime

DB_PATH = "data/discovered_asins.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
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
            updated_at TEXT
        )
    ''')

    conn.commit()
    conn.close()


def asin_exists(user_id, asin):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT asin FROM discovered_products WHERE user_id=? AND asin=?",
        (user_id, asin),
    )
    result = c.fetchone()
    conn.close()
    return result is not None


def insert_product(product):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO discovered_products (
            user_id,
            asin,
            title,
            marketplace,
            current_price,
            savings_percent,
            category,
            seller_name,
            seller_id,
            image,
            keepa_avg_90,
            keepa_drop_percent,
            ai_score,
            ai_reason,
            page_found,
            first_seen,
            last_seen,
            posted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
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
        product.get("first_seen", datetime.utcnow().isoformat()),
        datetime.utcnow().isoformat(),
        int(product.get("posted", False)),
    ))
    conn.commit()
    conn.close()


def get_products_for_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT * FROM discovered_products WHERE user_id=? ORDER BY last_seen DESC",
        (user_id,),
    )
    columns = [desc[0] for desc in c.description]
    rows = c.fetchall()
    conn.close()
    return [dict(zip(columns, row)) for row in rows]


def get_stats_for_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM discovered_products WHERE user_id=?",
        (user_id,),
    )
    total = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*) FROM discovered_products WHERE user_id=? AND posted=1",
        (user_id,),
    )
    posted = c.fetchone()[0]
    conn.close()
    return {"total_discovered": total, "total_posted": posted}


def clear_products_for_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM discovered_products WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_user_preferences(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM user_preferences WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {}
    columns = ["user_id", "keywords", "marketplaces", "min_saving", "max_price", "pages", "sort_by", "updated_at"]
    return dict(zip(columns, row))


def save_user_preferences(user_id, prefs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO user_preferences
            (user_id, keywords, marketplaces, min_saving, max_price, pages, sort_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        prefs.get("keywords", ""),
        prefs.get("marketplaces", "DE"),
        prefs.get("min_saving", 50),
        prefs.get("max_price", 450),
        prefs.get("pages", 1),
        prefs.get("sort_by", "Featured"),
        datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()


def reset_db():
    """Drop and recreate all tables (schema reset)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("DROP TABLE IF EXISTS discovered_products")
        c.execute("DROP TABLE IF EXISTS user_preferences")
        conn.commit()
    finally:
        conn.close()
    init_db()
