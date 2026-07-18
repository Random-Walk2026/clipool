from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from clipool import pool as poolmod
from clipool.account import Account
from clipool.server import app


class ModelsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        pool = poolmod.AccountPool()
        pool._accounts = {
            "codex": [
                Account(
                    "codex",
                    "pro",
                    home="/tmp/pro",
                    supported_models=frozenset({"gpt-a", "gpt-shared"}),
                ),
                Account(
                    "codex",
                    "team",
                    home="/tmp/team",
                    supported_models=frozenset({"gpt-b", "gpt-shared"}),
                ),
            ],
            "claude": [Account("claude", "c", token="token")],
        }
        pool._index = {"codex": 0, "claude": 0}
        pool._loaded = True
        self.previous_pool = poolmod._pool
        poolmod._pool = pool
        self.client = TestClient(app)

    def tearDown(self) -> None:
        poolmod._pool = self.previous_pool
        os.environ.pop("CLIPOOL_API_KEY", None)

    def test_lists_backend_defaults_and_discovered_codex_models(self) -> None:
        response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        by_id = {item["id"]: item for item in response.json()["data"]}

        self.assertIn("codex", by_id)
        self.assertIn("claude", by_id)
        self.assertEqual(by_id["gpt-a"]["accounts"], 1)
        self.assertEqual(by_id["gpt-b"]["accounts"], 1)
        self.assertEqual(by_id["gpt-shared"]["accounts"], 2)
        self.assertEqual(by_id["gpt-shared"]["capability"], "discovered")

    def test_models_endpoint_requires_configured_api_key(self) -> None:
        os.environ["CLIPOOL_API_KEY"] = "secret"
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        response = self.client.get(
            "/v1/models", headers={"Authorization": "Bearer secret"}
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
