#!/usr/bin/env python3
"""P0 regression tests (stdlib unittest)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vault_file_search import FindFilesParams, search_files
from vault_security import is_protected_resolved, resolve_under_vault


class TestExcludeMd(unittest.TestCase):
    def test_exclude_md_without_ext_returns_non_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("# a", encoding="utf-8")
            (root / "b.pdf").write_bytes(b"%PDF")
            out = search_files(root, FindFilesParams(exclude_md=True, limit=10))
            self.assertEqual(out["count"], 1)
            self.assertEqual(out["files"][0]["ext"], ".pdf")


class TestVaultSecurity(unittest.TestCase):
    def test_privado_in_resolved_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "_PRIVADO" / "x.md"
            secret.parent.mkdir()
            secret.write_text("secret", encoding="utf-8")
            _, err = resolve_under_vault(root, "_PRIVADO/x.md")
            self.assertIsNotNone(err)
            self.assertIn("_PRIVADO", err or "")

    def test_substring_bypass_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "not_PRIVADO_here.md").write_text("ok", encoding="utf-8")
            p, err = resolve_under_vault(root, "not_PRIVADO_here.md")
            self.assertIsNone(err)
            self.assertTrue(p is not None)


class TestPkce(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import os

        os.environ.setdefault("VAULT_PATH", "/tmp")
        os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret")
        os.environ.setdefault("OAUTH_PASSWORD", "unit-test-password")

    def test_s256_only(self) -> None:
        import obsidian_mcp as mcp

        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        self.assertTrue(mcp.pkce_verify(verifier, challenge, "S256"))
        self.assertFalse(mcp.pkce_verify(verifier, challenge, "plain"))


if __name__ == "__main__":
    unittest.main()
