#!/usr/bin/env python3
"""HTTP/OAuth smoke tests for the Starlette app."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient


class TestHttpE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._env = os.environ.copy()
        cls._tmpdir = tempfile.TemporaryDirectory()
        vault = Path(cls._tmpdir.name)
        (vault / "meta").mkdir()
        (vault / "_README.router.md").write_text("# router\n", encoding="utf-8")

        os.environ["VAULT_PATH"] = str(vault)
        os.environ["JWT_SECRET"] = "c" * 32
        os.environ["OAUTH_PASSWORD"] = "d" * 24
        os.environ["MCP_API_KEY"] = "test-api-key-12345"
        os.environ["MCP_BASE_URL"] = "http://testserver"
        os.environ["MCP_ALLOWED_ORIGINS"] = "*"

        import mcp_starter.server as srv

        srv._settings = None
        srv.bootstrap(require_vault=True)
        cls.srv = srv
        cls.client = TestClient(srv.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._tmpdir.cleanup()
        os.environ.clear()
        os.environ.update(cls._env)

    def test_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_oauth_metadata(self) -> None:
        r = self.client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(r.status_code, 200)
        self.assertIn("authorization_endpoint", r.json())

    def test_initialize_and_tools_list(self) -> None:
        init = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
            headers={"Authorization": "Bearer test-api-key-12345"},
        )
        self.assertEqual(init.status_code, 200)
        self.assertEqual(init.json()["result"]["protocolVersion"], "2025-11-25")

        tools = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Authorization": "Bearer test-api-key-12345"},
        )
        self.assertEqual(tools.status_code, 200)
        names = {t["name"] for t in tools.json()["result"]["tools"]}
        self.assertIn("vault_dispatch", names)

    def test_typed_skill_rejects_invalid_args(self) -> None:
        r = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "vault_dispatch",
                    "arguments": {"query": "test", "top": 99},
                },
            },
            headers={"Authorization": "Bearer test-api-key-12345"},
        )
        self.assertEqual(r.status_code, 200)
        payload = r.json()["result"]
        self.assertTrue(payload.get("isError"))
        self.assertIn("must be <= 5", payload["structuredContent"]["error"])

    def test_forbidden_origin_when_restricted(self) -> None:
        self.srv.ALLOWED_ORIGINS = frozenset({"https://allowed.example"})
        try:
            r = self.client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
                headers={
                    "Authorization": "Bearer test-api-key-12345",
                    "Origin": "https://evil.example",
                },
            )
            self.assertEqual(r.status_code, 403)
        finally:
            self.srv.ALLOWED_ORIGINS = None

    def test_authorize_form_get(self) -> None:
        r = self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "test",
                "redirect_uri": "http://127.0.0.1/cb",
                "code_challenge": "abc",
                "code_challenge_method": "S256",
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("Obsidian MCP", r.text)


if __name__ == "__main__":
    unittest.main()
