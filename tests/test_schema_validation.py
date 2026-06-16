#!/usr/bin/env python3
"""JSON Schema validation for typed skill manifests."""
from __future__ import annotations

import unittest

from mcp_starter import skill_registry


class TestSchemaValidation(unittest.TestCase):
    def test_rejects_top_above_maximum(self) -> None:
        root = __import__("pathlib").Path(__file__).resolve().parents[1] / "skills"
        manifest = next(
            m for m in skill_registry.discover_manifests(root) if m.name == "vault_dispatch"
        )
        errors = skill_registry.validate_tool_args(
            manifest.input_schema, {"query": "x", "top": 99}
        )
        self.assertTrue(any("must be <= 5" in e for e in errors))

    def test_rejects_unknown_properties(self) -> None:
        root = __import__("pathlib").Path(__file__).resolve().parents[1] / "skills"
        manifest = next(
            m for m in skill_registry.discover_manifests(root) if m.name == "vault_dispatch"
        )
        errors = skill_registry.validate_tool_args(
            manifest.input_schema, {"query": "x", "bogus": True}
        )
        self.assertTrue(any("unknown properties" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
