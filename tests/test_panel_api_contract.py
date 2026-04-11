import unittest
from pathlib import Path
import types
import sys
from unittest.mock import patch

if "httpx" not in sys.modules:
    class _DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            self.is_closed = False

        async def request(self, *args, **kwargs):
            return None

    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=_DummyAsyncClient, HTTPError=Exception, Response=object)

from services import panel_api


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class TestPanelApiMetadataContract(unittest.IsolatedAsyncioTestCase):
    async def test_set_user_metadata_uses_uuid_path_and_metadata_only_body(self):
        captured = {}

        async def fake_request(method, endpoint, panel_url, headers, verify_tls=True, json_data=None, params=None):
            captured["method"] = method
            captured["endpoint"] = endpoint
            captured["json_data"] = json_data
            return _Resp(200)

        with patch("services.panel_api.safe_api_request", new=fake_request):
            resp = await panel_api.set_user_metadata(
                "user-123",
                {"k": "v"},
                "https://panel.example/api",
                {"Authorization": "Bearer token"},
                True,
            )

        self.assertIsNotNone(resp)
        self.assertEqual(captured["method"], "PUT")
        self.assertIn("/metadata/user/user-123", captured["endpoint"])
        self.assertEqual(captured["json_data"], {"metadata": {"k": "v"}})
        self.assertNotIn("userUuid", captured["json_data"])

    def test_bot_sync_user_metadata_has_failed_response_logging_branch(self):
        source = Path("bot.py").read_text(encoding="utf-8")
        self.assertIn("resp.status_code >= 400", source)
        self.assertIn("sync_user_metadata panel rejected for %s: status=%s", source)


if __name__ == "__main__":
    unittest.main()
