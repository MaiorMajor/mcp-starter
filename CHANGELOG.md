# Changelog

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
