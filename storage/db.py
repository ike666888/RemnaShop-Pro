import sqlite3
import time
from typing import Any, Iterable, Optional


def init_db(db_file: str) -> None:
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, name TEXT, price TEXT, days INTEGER, gb INTEGER, reset_strategy TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, uuid TEXT, created_at TIMESTAMP)''')
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN plan_key TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    try:
        c.execute("ALTER TABLE plans ADD COLUMN reset_strategy TEXT DEFAULT 'NO_RESET'")
    except sqlite3.OperationalError:
        pass

    c.execute(
        '''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE,
            tg_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL,
            order_type TEXT NOT NULL,
            target_uuid TEXT,
            status TEXT NOT NULL,
            payment_text TEXT,
            admin_message_id INTEGER,
            menu_message_id INTEGER,
            waiting_message_id INTEGER,
            delivered_uuid TEXT,
            error_message TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )'''
    )

    c.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_tg_id ON subscriptions (tg_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_uuid ON subscriptions (uuid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_tg_id_status ON orders (tg_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders (order_id)")

    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_days', '3')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cleanup_days', '7')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_interval', '1')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_threshold', '50')")

    c.execute("SELECT count(*) FROM plans")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", ('p1', '1个月', '200元', 30, 100, 'NO_RESET'))
        c.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", ('p2', '3个月', '580元', 90, 500, 'NO_RESET'))

    conn.commit()
    conn.close()


def db_query(db_file: str, query: str, args: Iterable[Any] = (), one: bool = False):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, tuple(args))
    rv = cur.fetchall()
    conn.commit()
    conn.close()
    return (rv[0] if rv else None) if one else rv


def db_execute(db_file: str, query: str, args: Iterable[Any] = ()) -> int:
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute(query, tuple(args))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed
