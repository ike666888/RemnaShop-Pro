import unittest

from jobs.anomaly import build_anomaly_incidents
from jobs.expiry import should_send_expire_notice
from services.orders import classify_order_failure


class TestJobsAndOrders(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

