#!/usr/bin/env python3
"""Regression tests for v1.15 hardening fixes."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp_starter import vault_layout
from mcp_starter.config import load_settings


class TestOAuthPassword(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_rejects_default_oauth_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VAULT_PATH"] = tmp
            os.environ["JWT_SECRET"] = "a" * 32
            os.environ["OAUTH_PASSWORD"] = "change-me-before-exposing-publicly"
            with self.assertRaises(RuntimeError) as ctx:
                load_settings(require_vault=True)
            self.assertIn("OAUTH_PASSWORD", str(ctx.exception))


class TestSkillTimeout(unittest.TestCase):
    def test_timeout_expired_has_no_pid(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["sleep", "99"], timeout=1)
        self.assertFalse(hasattr(exc, "pid"))

    def test_run_skill_timeout_returns_controlled_error(self) -> None:
        import mcp_starter.server as srv

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "demo"
            skill_dir.mkdir()
            (skill_dir / "main.py").write_text("# noop\n")

            proc = MagicMock()
            proc.pid = 4242
            proc.communicate.side_effect = subprocess.TimeoutExpired(cmd=["demo"], timeout=45)

            srv._settings = None
            os.environ["VAULT_PATH"] = tmp
            os.environ["JWT_SECRET"] = "a" * 32
            os.environ["OAUTH_PASSWORD"] = "b" * 24
            srv.bootstrap(require_vault=True)
            srv.SKILLS_ROOT = root

            with patch("mcp_starter.server.subprocess.Popen", return_value=proc):
                with patch("os.getpgid", return_value=4242):
                    with patch("os.killpg") as mock_killpg:
                        out = srv._run_skill("demo")

        self.assertEqual(out.get("error"), "Skill 'demo' timed out after 45s")
        mock_killpg.assert_called_once_with(4242, signal.SIGKILL)


class TestVaultLayout(unittest.TestCase):
    def test_dispatch_fallback_matches_init(self) -> None:
        self.assertEqual(vault_layout.CAPTURE_FALLBACK, "inbox/")
        self.assertIn("inbox", vault_layout.INIT_DIRS)
        self.assertIn("meta", vault_layout.INIT_DIRS)


if __name__ == "__main__":
    unittest.main()
