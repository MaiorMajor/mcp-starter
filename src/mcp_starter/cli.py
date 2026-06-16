"""CLI: mcp-starter init | serve | graph-build"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

from mcp_starter import __version__
from mcp_starter.paths import find_repo_root
from mcp_starter.vault_layout import INIT_DIRS

ROUTER_TEMPLATE = """---
title: Vault Router
type: meta
---

# Vault router

Minimal routing hints for MCP agents. Expand as your vault grows.

## Guardrails

- `_PRIVADO/` — never read or write via MCP
- `inbox/` — capture only; promote notes to proper folders
- Confirm before bulk deletes or moves

## Folders

| Folder | Purpose |
|--------|---------|
| `inbox/` | Quick capture |
| `work/` | Active projects |
| `personal/` | Life admin |
| `research/` | Reading & references |
| `meta/` | Graph snapshot, rules |
"""

CONTEXT_TEMPLATE = """---
title: Work
keywords: [work, projects]
---

# Work

Routing context for work-related notes. Add keywords and routing rows as your vault grows.

| Task type | Destination |
|-----------|-------------|
| New idea | `inbox/` |
| Active project | `work/projects/` |
"""

ENV_TEMPLATE = """VAULT_PATH={vault_path}
MCP_API_KEY={mcp_api_key}
JWT_SECRET={jwt_secret}
OAUTH_PASSWORD={oauth_password}
MCP_BASE_URL=https://your-domain.com
"""


def cmd_init(vault_path: Path, force: bool) -> int:
    vault_path = vault_path.resolve()
    repo_root = find_repo_root()

    if vault_path.exists() and any(vault_path.iterdir()) and not force:
        print(f"Vault not empty: {vault_path} (use --force to continue)", file=sys.stderr)
        return 1

    for name in INIT_DIRS:
        (vault_path / name).mkdir(parents=True, exist_ok=True)

    router = vault_path / "_README.router.md"
    if not router.exists() or force:
        router.write_text(ROUTER_TEMPLATE, encoding="utf-8")

    ctx = vault_path / "work" / "CONTEXT.md"
    if not ctx.exists() or force:
        ctx.write_text(CONTEXT_TEMPLATE, encoding="utf-8")

    env_path = repo_root / ".env"
    if not env_path.exists():
        env_path.write_text(
            ENV_TEMPLATE.format(
                vault_path=vault_path,
                mcp_api_key=secrets.token_hex(32),
                jwt_secret=secrets.token_hex(32),
                oauth_password=secrets.token_urlsafe(24),
            ),
            encoding="utf-8",
        )
        print(f"Created {env_path} with generated secrets")
    else:
        print(f"Existing .env kept at {env_path} — set VAULT_PATH={vault_path} if needed")

    print(f"Vault ready at {vault_path}")
    print("Next: pip install -e '.[dev]' && mcp-starter serve")
    return 0


def cmd_serve(host: str, port: int) -> int:
    from mcp_starter.server import main as serve_main

    serve_main(host=host, port=port)
    return 0


def cmd_graph_build(vault: Path | None) -> int:
    repo_root = find_repo_root()
    skill = repo_root / "skills" / "vault-graph" / "main.py"
    if not skill.exists():
        print("vault-graph skill not found", file=sys.stderr)
        return 1
    env = os.environ.copy()
    if vault:
        env["VAULT_PATH"] = str(vault.resolve())
    proc = subprocess.run([sys.executable, str(skill)], env=env)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-starter", description="Obsidian vault MCP server")
    parser.add_argument("--version", action="version", version=f"mcp-starter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create minimal vault structure and .env")
    p_init.add_argument("vault_path", type=Path, help="Path to your Obsidian vault")
    p_init.add_argument("--force", action="store_true", help="Overwrite scaffold files")

    p_serve = sub.add_parser("serve", help="Run the MCP server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)

    p_graph = sub.add_parser("graph-build", help="Build vault-graph snapshot")
    p_graph.add_argument("--vault", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.vault_path, args.force)
    if args.command == "serve":
        return cmd_serve(args.host, args.port)
    if args.command == "graph-build":
        return cmd_graph_build(args.vault)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
