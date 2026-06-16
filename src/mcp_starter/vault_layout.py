"""Canonical vault folder layout for the public starter (no numeric prefixes)."""

from __future__ import annotations

INBOX = "inbox"
WORK = "work"
PERSONAL = "personal"
RESEARCH = "research"
META = "meta"
PRIVADO = "_PRIVADO"

CAPTURE_FALLBACK = f"{INBOX}/"
GRAPH_JSON = f"{META}/vault-graph.json"
GRAPH_STALE = f"{META}/.vault-graph-stale"
AGENT_PAUSE = f"{META}/AGENT_PAUSE"

INIT_DIRS: tuple[str, ...] = (
    INBOX,
    WORK,
    f"{WORK}/projects",
    PERSONAL,
    RESEARCH,
    META,
    PRIVADO,
)

WEAK_OAUTH_PASSWORDS: frozenset[str] = frozenset(
    {
        "",
        "change-me-before-exposing-publicly",
        "your-oauth-password",
    }
)
