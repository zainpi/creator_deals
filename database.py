import sqlite3
from datetime import datetime

DB_PATH = "data/discovered_asins.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS discovered_products (
            asin TEXT PRIMARY KEY,
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
            posted INTEGER DEFAULT 0
        )
    ''')

    # Backfill/migrate: add missing columns if table existed before
    try:
        c.execute("PRAGMA table_info('discovered_products')")
        cols = [row[1] for row in c.fetchall()]
        if 'image' not in cols:
            c.execute("ALTER TABLE discovered_products ADD COLUMN image TEXT")
        if 'page_found' not in cols:
            c.execute("ALTER TABLE discovered_products ADD COLUMN page_found INTEGER")
    except Exception:
        pass

    conn.commit()
    conn.close()


def asin_exists(asin):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT asin FROM discovered_products WHERE asin=?", (asin,))
    result = c.fetchone()

    conn.close()
    return result is not None


def insert_product(product):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        INSERT OR REPLACE INTO discovered_products (
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
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
        int(product.get("posted", False))
    ))

    conn.commit()
    conn.close()


def get_all_products():
    """Get all discovered products for dashboard."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT * FROM discovered_products ORDER BY last_seen DESC")
    columns = [desc[0] for desc in c.description]
    rows = c.fetchall()
    
    conn.close()
    
    return [dict(zip(columns, row)) for row in rows]


def get_stats():
    """Get discovery stats."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) as total FROM discovered_products")
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) as posted FROM discovered_products WHERE posted=1")
    posted = c.fetchone()[0]
    
    conn.close()
    
    return {
        "total_discovered": total,
        "total_posted": posted
    }


def clear_all_products():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM discovered_products")
    conn.commit()
    conn.close()


def reset_db():
    """Drop and recreate the discovered_products table to refresh schema."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("DROP TABLE IF EXISTS discovered_products")
        conn.commit()
    finally:
        conn.close()
    # Recreate
    init_db()
