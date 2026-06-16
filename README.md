# mcp-starter

> Turn any Obsidian vault (or plain markdown folder) into a typed, agent-native second brain — served over the Model Context Protocol, with deterministic routing that spends **zero LLM tokens** to decide where things go.

[![status](https://img.shields.io/badge/status-public%20beta-1.16.0-success)](https://github.com/MaiorMajor/mcp-starter/releases)
[![protocol](https://img.shields.io/badge/MCP-2025--11--25-blue)](https://modelcontextprotocol.io)
[![python](https://img.shields.io/badge/python-3.11+-3776ab)](https://www.python.org)
[![transport](https://img.shields.io/badge/transport-SSE%20%2B%20Streamable%20HTTP-orange)]()
[![license](https://img.shields.io/badge/license-MIT-lightgrey)]()

This is the open-source skeleton of an MCP server that has been running in production for **6 months**. This repo ships **14 core MCP tools** plus **3 typed skill tools** (`vault_dispatch`, `vault_find`, `vault_graph`) — the same architecture scales to dozens of typed skills over a real Obsidian vault.

**Current release:** [v1.16.0](https://github.com/MaiorMajor/mcp-starter/releases/latest) (public beta). See [CHANGELOG.md](./CHANGELOG.md).

---

## Why this is different

Most "MCP for Obsidian" servers are a thin wrapper around `read_file` / `write_file`. They make the LLM do all the thinking: *which* folder? *which* note? *is this already answered somewhere?* — and every one of those decisions burns context window and money.

`mcp-starter` is built on one inversion: **the LLM is the most expensive resource, so push every decision you can out of it and into deterministic Python.**

| Naive MCP server | mcp-starter |
|---|---|
| `read_file`, `write_file`, `search` | 14 tools + **typed skills** discovered by name |
| LLM decides where notes go | `vault-dispatch` routes by pattern matching — **0 tokens** |
| Dumps whole files into context | Per-tool caps + truncation **hints** that name the cheaper next tool |
| Backlinks = read the whole graph | `vault-graph` answers from a 1.8MB snapshot **never shown to the model** |
| Behaviour baked into code | System prompt is a **repo file** (`system_prompt.md`) — change agent behaviour with no deploy |
| Search = scan everything every time | `vault-find` / `find_files` return metadata without reading file bodies |

The vault is your content (`VAULT_PATH`). Skills, routing hints, and the system prompt live in **this repo** — auditable, versioned, and extensible without forking the server.

---

## Install (≤ 3 steps)

```bash
# 1. Clone and install (editable — skills + system prompt stay at repo root)
git clone https://github.com/MaiorMajor/mcp-starter.git && cd mcp-starter
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Scaffold a vault and generate .env secrets
mcp-starter init /path/to/your/vault
# or: cp .env.example .env and set VAULT_PATH, MCP_API_KEY, JWT_SECRET, OAUTH_PASSWORD

# 3. Run
mcp-starter serve                    # :8000
curl localhost:8000/health           # {"status":"ok",...}
```

`obsidian_mcp.py` remains as a thin back-compat shim (`python obsidian_mcp.py`).

Then point any MCP client at it with `Authorization: Bearer $MCP_API_KEY` (or the OAuth/PKCE flow for Claude.ai). Behind nginx + a domain you get `https://your-host/mcp`.

---

## Quickstart: adapt it to your vault

The server is vault-agnostic — it only assumes a top-level folder convention you can change. Defaults:

```
inbox/           # immutable capture zone — read & promote, never edit in place
work/            # active projects (add subfolders as you grow)
personal/        # life admin
research/        # reading & references
meta/            # graph snapshot, changelog, rules
_PRIVADO/        # blind to MCP — never listed, read, or written
```

`mcp-starter init` scaffolds exactly this layout.

1. Set `VAULT_PATH` in `.env`.
2. Edit `skill_hints.json` in this repo → keywords that map to *your* folders and skills, so `vault-dispatch` knows where your topics live.
3. Edit `system_prompt.md` at the repo root. It's loaded on every `initialize` — no redeploy to change agent behaviour.
4. (Optional) Build the link snapshot: `mcp-starter graph-build` or `python skills/vault-graph/main.py`.

That's it — `read_note`, `write_note`, `edit_note`, `find_files`, `vault-dispatch` work immediately on your content.

---

## The 14 tools

`session_start` · `list_folder` · `find_files` · `read_note` · `read_frontmatter` · `bulk_read` (≤20) · `read_json` · `read_jsonl` · `write_note` (create/update/append/upsert) · `edit_note` (surgical, `expect_count`) · `move_note` · `search_notes` (capped, last-resort) · `get_current_datetime` · `run_skill`

Every tool carries the right MCP `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`) so clients can auto-approve the safe ones. Read paths are deliberately over-provided (six ways to read, each a different point on the cost/precision curve); the write surface is minimal and auditable.

---

## Included example skills (typed MCP tools)

Each skill ships a `manifest.json` that registers a **real MCP tool** with `inputSchema` — clients see `vault_dispatch(query, top)` instead of only `run_skill("vault-dispatch", [...])`.

| MCP tool | Skill dir | What it does |
|----------|-----------|----------------|
| `vault_dispatch` | `vault-dispatch/` | "Where does this go?" — **0 LLM tokens** |
| `vault_find` | `vault-find/` | File metadata scan (name, ext, date) |
| `vault_graph` | `vault-graph/` | Backlinks, hubs, orphans — graph hidden |

`run_skill` remains for extension skills you add under `skills/` with `main.py` + optional `manifest.json`.

---

## Architecture (text diagram)

```
        MCP client (Claude / ChatGPT / Open WebUI)
                       │  JSON-RPC over
        ┌──────────────┴───────────────┐
        │  SSE /messages   Streamable /mcp │   ← dual transport, version-negotiated
        └──────────────┬───────────────┘
                       │  OAuth 2.0 PKCE  ·  Bearer (static key or 15m access JWT)
        ┌──────────────▼───────────────────────────────────────┐
        │  mcp_starter.server  (Starlette + uvicorn)              │
        │  • 14 tools  • caps + truncation hints               │
        │  • system prompt from system_prompt.md on initialize │
        └──────────────┬───────────────────────────────────────┘
                       │ VAULT_PATH (your markdown vault)
        ┌──────────────▼───────────────────────────────────────┐
        │  YOUR VAULT  (markdown notes, graph snapshot)         │
        │                                                       │
        │  vault-dispatch ──► query → destination  (0 LLM tokens)│
        │  vault-find     ──► metadata scan, no content read     │
        │  vault-graph    ──► backlinks/hubs/orphans (hidden JSON)│
        │  skill_hints.json + system_prompt.md  (repo root)      │
        └───────────────────────────────────────────────────────┘
```

**Token discipline as a first-class feature:** when a response is truncated, it returns a `hint` naming the cheaper tool to use next (`"Results capped at 30. Refine query; for recent files use vault-find"`). Tool descriptions are deliberate prompt engineering (`"LAST RESORT full-text search"`, `"Call ONCE per conversation"`). The graph JSON is **never** exposed — only query subcommands are.

---

## Deploy (VPS + systemd + Syncthing)

```ini
# /etc/systemd/system/mcp-obsidian.service
[Service]
ExecStart=/home/you/mcp-env/bin/mcp-starter serve
Restart=always
EnvironmentFile=/home/you/mcp-server/.env
```

```bash
sudo systemctl enable --now mcp-obsidian
curl https://your-host/mcp-health   # behind nginx with X-Accel-Buffering off for SSE
```

- **Syncthing** keeps the vault on your laptop, phone and VPS in sync — the server just watches `VAULT_PATH`. No database, no git push to deploy content; you edit a note anywhere and the agent sees it.
- nginx terminates TLS and proxies `/sse`, `/messages`, `/mcp`; set `X-Accel-Buffering: no` so SSE streams aren't buffered.
- The OAuth metadata endpoints (`/.well-known/oauth-*`) make it a first-class connector for Claude.ai and ChatGPT developer mode.

> **Security (v1.16):** OAuth clients are persisted (`oauth_clients.json`). Redirect URIs are validated by exact match. PKCE is S256-only. Access tokens expire in 15 minutes (configurable). SSE requires authentication. By default `MCP_ALLOWED_ORIGINS` is restrictive (your `MCP_BASE_URL` + known MCP clients); set `MCP_ALLOWED_ORIGINS=*` for local dev. Before exposing publicly, set strong secrets and register client redirect URIs via `POST /register` or `OAUTH_REDIRECT_URIS` in `.env`.

---

## Links

- **Case study:** [`CASE-STUDY.md`](./CASE-STUDY.md) — 6 months from notes app to a production MCP system
- **Author:** [Jorge MM Marques](https://github.com/MaiorMajor) — AI Engineer · Agentic Systems · Python Automation

MIT. Clone this repo, point `VAULT_PATH` at your Obsidian vault, and extend `skills/` as needed.
