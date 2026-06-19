# Changelog

## Unreleased

### Changed
- Repositioned the project under the **AgentVault** product name: a bounded, deterministic MCP runtime for Obsidian and Markdown knowledge bases.
- Rewrote the README around the user problem, target audience, product boundary and local-first evaluation flow.
- Separated local evaluation from production deployment requirements.
- Clarified that the public repository contains the reusable runtime and three representative skills, not the author's private 47-skill deployment.
- Corrected the source-of-truth model: the vault owns content; the repository owns runtime behaviour, skills, routing hints and the system prompt.
- Reframed “zero-token routing” as deterministic routing for explicit taxonomies rather than a replacement for model reasoning.
- Updated package metadata and discovery keywords while retaining the existing package and CLI names for compatibility.

## [1.16.0] — 2026-06-16

### Added
- JSON Schema validation for typed skill tool arguments at runtime
- Restrictive default `MCP_ALLOWED_ORIGINS` (override with `*` to allow all)
- HTTP/OAuth smoke tests (`tests/test_http.py`)
- `asyncio.to_thread` for `tools/call` so skill subprocesses do not block the event loop

### Changed
- Beta release: see [v1.16.0](https://github.com/MaiorMajor/mcp-starter/releases/tag/v1.16.0)

## [1.15.1] — 2026-06-16

### Fixed
- `vault-find` table output (header/rows after mistaken indent)
- `vault-graph` `REPO_ROOT` (`parents[1]` not `parents[2]`)
- Skill timeout on Windows (`proc.kill` + reap)

## [1.15.0] — 2026-06-16

### Added
- Installable package (`pip install -e ".[dev]"`), CLI (`mcp-starter init|serve|graph-build`)
- Canonical vault layout (`vault_layout.py`)
- GitHub Actions CI (Python 3.11–3.13)

### Fixed
- Skill timeout (`Popen` + process-group kill)
- OAuth weak default password rejected at startup
- Typed skills + MCP 2025-11-25 transport hardening (from 1.13.x line)

## [1.13.0] and earlier

See git history.