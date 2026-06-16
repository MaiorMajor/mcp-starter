"""Runtime configuration loaded from environment and .env at repo root."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from mcp_starter.paths import find_repo_root
from mcp_starter.vault_layout import WEAK_OAUTH_PASSWORDS


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    vault_path: Path
    mcp_api_key: str
    oauth_password: str
    jwt_secret: str
    base_url: str
    skills_root: Path
    access_token_ttl: int
    refresh_token_ttl: int
    oauth_clients_file: Path
    oauth_rate_limit: int
    oauth_rate_window: int
    allowed_origins: frozenset[str]
    server_version: str = "1.15.0"
    default_protocol_version: str = "2025-11-25"
    supported_protocol_versions: tuple[str, ...] = (
        "2025-11-25",
        "2025-03-26",
        "2024-11-05",
    )


def load_settings(*, require_vault: bool = True) -> Settings:
    repo_root = find_repo_root()
    load_dotenv(repo_root / ".env")

    vault_raw = os.getenv("VAULT_PATH", "").strip()
    if require_vault and not vault_raw:
        raise RuntimeError(
            "VAULT_PATH must be set in .env or the environment. "
            "Run: mcp-starter init /path/to/vault"
        )

    jwt_secret = os.getenv("JWT_SECRET", "change-me-in-production")
    oauth_password = os.getenv("OAUTH_PASSWORD", "")
    if require_vault:
        if not jwt_secret.strip() or jwt_secret == "change-me-in-production":
            raise RuntimeError("JWT_SECRET must be set to a strong secret in production.")
        if oauth_password.strip() in WEAK_OAUTH_PASSWORDS:
            raise RuntimeError(
                "OAUTH_PASSWORD must be set to a strong secret in production "
                "(run mcp-starter init or set a random password in .env)."
            )

    return Settings(
        repo_root=repo_root,
        vault_path=Path(vault_raw) if vault_raw else Path("/tmp"),
        mcp_api_key=os.getenv("MCP_API_KEY", ""),
        oauth_password=oauth_password,
        jwt_secret=jwt_secret,
        base_url=os.getenv("MCP_BASE_URL", "https://your-domain.com").rstrip("/"),
        skills_root=repo_root / "skills",
        access_token_ttl=int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900")),
        refresh_token_ttl=int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(30 * 24 * 3600))),
        oauth_clients_file=Path(
            os.getenv("OAUTH_CLIENTS_FILE", str(repo_root / "oauth_clients.json"))
        ),
        oauth_rate_limit=int(os.getenv("OAUTH_RATE_LIMIT", "30")),
        oauth_rate_window=int(os.getenv("OAUTH_RATE_WINDOW_SECONDS", "300")),
        allowed_origins=frozenset(
            o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
        ),
    )
