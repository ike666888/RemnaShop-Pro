import unittest
import sqlite3

from jobs.anomaly import build_anomaly_incidents
from jobs.expiry import should_send_expire_notice
from services.orders import STATUS_PENDING, classify_order_failure, create_order


class TestJobsAndOrders(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE orders (
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
                updated_at INTEGER NOT NULL,
                channel_code TEXT
            )"""
        )

    def tearDown(self):
        self.conn.close()

    def _db_query(self, query, args=(), one=False):
        cur = self.conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        return (rv[0] if rv else None) if one else rv

    def _db_execute(self, query, args=()):
        cur = self.conn.cursor()
        cur.execute(query, args)
        self.conn.commit()
        return cur.rowcount

    def test_should_send_expire_notice(self):
        self.assertTrue(should_send_expire_notice(None, 200))
        self.assertFalse(should_send_expire_notice(190, 200, cool_down_seconds=20))
        self.assertTrue(should_send_expire_notice(100, 200, cool_down_seconds=20))

    def test_build_anomaly_incidents(self):
        logs = [
            {"_ts": 101, "userUuid": "u1", "requestIp": "1.1.1.1", "userAgent": "a", "_fmt_time": "t1"},
            {"_ts": 102, "userUuid": "u1", "requestIp": "1.1.1.2", "userAgent": "b", "_fmt_time": "t2"},
            {"_ts": 103, "userUuid": "u2", "requestIp": "2.2.2.2", "userAgent": "x", "_fmt_time": "t3"},
        ]
        incidents, max_ts = build_anomaly_incidents(logs, last_scan_ts=100, whitelist=set(), ip_threshold=1)
        self.assertEqual(max_ts, 103)
        self.assertTrue(any(item["uid"] == "u1" for item in incidents))

    def test_classify_order_failure(self):
        self.assertEqual(classify_order_failure("timeout from api"), "network")
        self.assertEqual(classify_order_failure("sqlite constraint failed"), "database")
        self.assertEqual(classify_order_failure("invalid plan"), "business_validation")

    def test_create_order_not_reuse_when_plan_differs(self):
        first, created_first = create_order(self._db_query, self._db_execute, 1001, "p1", "new", "0")
        second, created_second = create_order(self._db_query, self._db_execute, 1001, "p2", "new", "0")
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(first["order_id"], second["order_id"])

    def test_create_order_reuse_only_when_plan_type_target_match(self):
        first, created_first = create_order(self._db_query, self._db_execute, 1002, "p1", "renew", "uuid-a")
        second, created_second = create_order(self._db_query, self._db_execute, 1002, "p1", "renew", "uuid-a")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(second["status"], STATUS_PENDING)

    def test_create_order_not_reuse_when_target_uuid_differs(self):
        first, created_first = create_order(self._db_query, self._db_execute, 1003, "p1", "renew", "uuid-a")
        second, created_second = create_order(self._db_query, self._db_execute, 1003, "p1", "renew", "uuid-b")
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(first["order_id"], second["order_id"])


if __name__ == "__main__":
    unittest.main()
