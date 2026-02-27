import sqlite3
from typing import Any, Iterable


def _connect(db_file: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_file: str) -> None:
    conn = _connect(db_file)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS plans (key TEXT PRIMARY KEY, name TEXT, price TEXT, days INTEGER, gb INTEGER, reset_strategy TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, uuid TEXT, created_at TIMESTAMP)''')
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN plan_key TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')

    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN last_notify_expire_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN last_notify_days_left INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE subscriptions ADD COLUMN last_notify_at INTEGER")
    except sqlite3.OperationalError:
        pass
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


    try:
        c.execute("ALTER TABLE orders ADD COLUMN channel_code TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS anomaly_whitelist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_uuid TEXT UNIQUE,
        created_at INTEGER NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS order_audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        action TEXT NOT NULL,
        actor_id INTEGER,
        detail TEXT,
        created_at INTEGER NOT NULL
    )''')


    c.execute('''CREATE TABLE IF NOT EXISTS bulk_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        result_json TEXT,
        created_by INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ops_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_by INTEGER,
        created_at INTEGER NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS anomaly_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_uuid TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        risk_score INTEGER NOT NULL,
        ip_count INTEGER NOT NULL,
        ua_diversity INTEGER NOT NULL,
        density INTEGER NOT NULL,
        action_taken TEXT NOT NULL,
        evidence_summary TEXT,
        created_at INTEGER NOT NULL
    )''')

    c.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_tg_id ON subscriptions (tg_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_uuid ON subscriptions (uuid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_tg_id_status ON orders (tg_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders (order_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders (status, created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_order_audit_order_id ON order_audit_logs (order_id, created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_anomaly_events_user_created ON anomaly_events (user_uuid, created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bulk_jobs_status_created ON bulk_jobs (status, created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ops_templates_created ON ops_templates (created_at DESC)")

    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_days', '3')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cleanup_days', '7')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_interval', '1')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_threshold', '50')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('risk_low_score', '80')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('risk_high_score', '130')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('risk_enforce_mode', 'enforce')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('anomaly_last_scan_ts', '0')")

    c.execute("SELECT count(*) FROM plans")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", ('p1', '1个月', '200元', 30, 100, 'NO_RESET'))
        c.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)", ('p2', '3个月', '580元', 90, 500, 'NO_RESET'))

    conn.commit()
    conn.close()


def db_query(db_file: str, query: str, args: Iterable[Any] = (), one: bool = False):
    conn = _connect(db_file)
    cur = conn.cursor()
    cur.execute(query, tuple(args))
    rv = cur.fetchall()
    conn.close()
    return (rv[0] if rv else None) if one else rv


def db_execute(db_file: str, query: str, args: Iterable[Any] = ()) -> int:
    conn = _connect(db_file)
    cur = conn.cursor()
    cur.execute(query, tuple(args))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed
