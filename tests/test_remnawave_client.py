import json
import tempfile
import unittest

from services.remnawave_client import RemnawaveApiClient


class TestRemnawaveClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False)
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/auth/login": {
                    "post": {
                        "operationId": "authLogin",
                        "tags": ["auth"],
                    }
                },
                "/users/{uuid}": {
                    "get": {
                        "operationId": "getUserByUuid",
                        "tags": ["users"],
                        "parameters": [{"name": "uuid", "in": "path", "required": True}],
                    }
                },
            },
        }
        json.dump(spec, self.tmp)
        self.tmp.flush()

    async def test_load_and_filter_operations(self):
        client = RemnawaveApiClient(base_url="https://example.com/api", spec_path=self.tmp.name)
        self.assertEqual(len(client.get_auth_operations()), 1)
        self.assertEqual(len(client.get_user_operations()), 1)
        await client.aclose()

    async def test_guard_non_matching_tag(self):
        client = RemnawaveApiClient(base_url="https://example.com/api", spec_path=self.tmp.name)
        with self.assertRaises(ValueError):
            await client.call_auth_operation("getUserByUuid")
        await client.aclose()


if __name__ == "__main__":
    unittest.main()

