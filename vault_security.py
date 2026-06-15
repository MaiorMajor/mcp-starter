"""Vault path resolution and protected-path checks (resolved components, not substrings)."""
from __future__ import annotations

from pathlib import Path

PROTECTED_DIR_NAMES = frozenset({"_PRIVADO"})


def vault_root(vault: Path) -> Path:
    return vault.resolve()


def is_protected_resolved(vault: Path, resolved: Path) -> bool:
    root = vault_root(vault)
    try:
        rel = resolved.resolve().relative_to(root)
    except ValueError:
        return True
    return bool(PROTECTED_DIR_NAMES & set(rel.parts))


def resolve_under_vault(vault: Path, rel_path: str) -> tuple[Path | None, str | None]:
    if not rel_path:
        return None, "path is required"
    root = vault_root(vault)
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, "Path traversal not allowed"
    if is_protected_resolved(vault, target):
        return None, "Access denied: _PRIVADO/ contents are never exposed via MCP."
    return target, None
