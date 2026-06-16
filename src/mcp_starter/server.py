#!/usr/bin/env python3
"""
MCP Server for Obsidian Vault with OAuth 2.0 PKCE (single-user)
Stack: Starlette + uvicorn + sse-starlette

Environment variables (loaded from .env next to this file):
  MCP_API_KEY      – static API key accepted as Bearer token
  VAULT_PATH       – absolute path to the Obsidian vault (required)
  OAUTH_PASSWORD   – plain-text password for the /authorize form
  JWT_SECRET       – secret used to sign / verify JWT tokens
  MCP_BASE_URL     – public URL for OAuth metadata (default: https://your-domain.com)

Endpoints:
  GET  /authorize      HTML login form (PKCE params passed as query string)
  POST /authorize      Validate password → issue auth code → redirect
  POST /token          Exchange auth code + code_verifier for tokens
  POST /token/refresh  Exchange refresh token for new access token
  GET  /health         Public health check
  GET  /sse            MCP SSE transport  (Bearer JWT or MCP_API_KEY)
  POST /messages       MCP message endpoint (Bearer JWT or MCP_API_KEY)
  POST /register       Dynamic Client Registration
  GET  /.well-known/oauth-authorization-server   OAuth metadata
  GET  /.well-known/oauth-protected-resource     OAuth resource metadata

MCP Tools: session_start, list_folder, find_files, read_note, read_frontmatter,
bulk_read, read_json, read_jsonl, write_note, edit_note, move_note, search_notes,
get_current_datetime, run_skill
"""

import asyncio
import base64
import copy
import hashlib
import html
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import jwt as pyjwt  # PyJWT
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_starter import skill_registry
from mcp_starter import vault_file_search as vfs
from mcp_starter import vault_json_reader as vjr
from mcp_starter import vault_security as vsec
from mcp_starter.config import Settings, load_settings
from mcp_starter.paths import find_repo_root
from mcp_starter.vault_layout import AGENT_PAUSE, GRAPH_JSON, GRAPH_STALE, INBOX, META

# ─── Configuration (populated by bootstrap()) ─────────────────────────────────

_settings: Settings | None = None
REPO_ROOT: Path
VAULT_PATH: Path
MCP_API_KEY: str
OAUTH_PASSWORD: str
JWT_SECRET: str
BASE_URL: str
SKILLS_ROOT: Path
ACCESS_TOKEN_TTL: int
REFRESH_TOKEN_TTL: int
OAUTH_CLIENTS_FILE: Path
OAUTH_RATE_LIMIT: int
OAUTH_RATE_WINDOW: int
ALLOWED_ORIGINS: frozenset[str]
SERVER_VERSION: str = "1.15.0"
DEFAULT_PROTOCOL_VERSION: str = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25",
    "2025-03-26",
    "2024-11-05",
)
PROTECTED_METHODS: set[str] = {"tools/call"}
SKILL_MANIFESTS: list[skill_registry.SkillManifest] = []
SKILL_MANIFEST_BY_NAME: dict[str, skill_registry.SkillManifest] = {}


def bootstrap(require_vault: bool = True) -> Settings:
    global _settings, REPO_ROOT, VAULT_PATH, MCP_API_KEY, OAUTH_PASSWORD, JWT_SECRET
    global BASE_URL, SKILLS_ROOT, ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL
    global OAUTH_CLIENTS_FILE, OAUTH_RATE_LIMIT, OAUTH_RATE_WINDOW, ALLOWED_ORIGINS
    global SKILL_MANIFESTS, SKILL_MANIFEST_BY_NAME

    if _settings is not None:
        return _settings

    _settings = load_settings(require_vault=require_vault)
    REPO_ROOT = _settings.repo_root
    VAULT_PATH = _settings.vault_path
    MCP_API_KEY = _settings.mcp_api_key
    OAUTH_PASSWORD = _settings.oauth_password
    JWT_SECRET = _settings.jwt_secret
    BASE_URL = _settings.base_url
    SKILLS_ROOT = _settings.skills_root
    ACCESS_TOKEN_TTL = _settings.access_token_ttl
    REFRESH_TOKEN_TTL = _settings.refresh_token_ttl
    OAUTH_CLIENTS_FILE = _settings.oauth_clients_file
    OAUTH_RATE_LIMIT = _settings.oauth_rate_limit
    OAUTH_RATE_WINDOW = _settings.oauth_rate_window
    ALLOWED_ORIGINS = _settings.allowed_origins

    SKILL_MANIFESTS = skill_registry.discover_manifests(SKILLS_ROOT)
    SKILL_MANIFEST_BY_NAME = {m.name: m for m in SKILL_MANIFESTS}
    _validate_skill_dirs(SKILLS_ROOT)

    global refresh_tokens, oauth_clients
    refresh_tokens = _load_refresh_tokens()
    oauth_clients = _load_oauth_clients()
    return _settings


def _validate_skill_dirs(skills_root: Path) -> None:
    seen: dict[str, Path] = {}
    for main_py in skills_root.rglob("main.py"):
        if "_shared" in main_py.parts:
            continue
        name = main_py.parent.name
        if name in seen:
            raise RuntimeError(
                f"Duplicate skill name '{name}': {seen[name]} and {main_py.parent}"
            )
        seen[name] = main_py.parent


# Response size caps (all MCP clients)
SEARCH_MAX_RESULTS: int = 30
FIND_FILES_MAX: int = 100
LIST_RECURSIVE_MAX: int = 200
LIST_INBOX_MAX: int = 100
READ_NOTE_MAX_CHARS: int = 32_000
RUN_SKILL_STDOUT_MAX: int = 16_000
SEARCH_MIN_QUERY_LEN: int = 2
SEARCH_TIMEOUT_SEC: float = 30.0
_SEARCH_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_TRUNCATE_SUFFIX = "\n\n[... truncated — use read_frontmatter, vault-find, or a narrower path ...]"
_SKILL_TRUNCATE_SUFFIX = "\n\n[... stdout truncated by MCP server ...]"

_logger = logging.getLogger("obsidian_mcp")
if not _logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
_logger.setLevel(logging.INFO)


def _tokens_file() -> Path:
    env = os.getenv("TOKENS_FILE", "").strip()
    if env:
        return Path(env)
    return find_repo_root() / "refresh_tokens.json"


def _truncate_text(text: str, max_chars: int, suffix: str = _TRUNCATE_SUFFIX) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + suffix, True


def _result_is_truncated(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("truncated") or result.get("stdout_truncated") or result.get("stderr_truncated"):
        return True
    if isinstance(result.get("notes"), dict):
        return any(
            isinstance(v, dict) and v.get("truncated")
            for v in result["notes"].values()
        )
    return bool(result.get("content_truncated"))


def _log_tool_call(tool_name: str, result: Any) -> None:
    try:
        payload = json.dumps(result, ensure_ascii=False)
        nbytes = len(payload.encode("utf-8"))
    except (TypeError, ValueError):
        nbytes = 0
    truncated = _result_is_truncated(result)
    _logger.info("mcp_tool_call tool=%s bytes=%s truncated=%s", tool_name, nbytes, truncated)


# ─── Persistent stores (atomic JSON writes) ───────────────────────────────────

_oauth_rate_hits: dict[str, list[float]] = {}


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_json_store(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_refresh_tokens() -> dict[str, dict]:
    data = _load_json_store(_tokens_file())
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("expires", 0) > now}


def _save_refresh_tokens(tokens: dict[str, dict]) -> None:
    try:
        _atomic_write_json(_tokens_file(), tokens)
    except OSError:
        pass


def _load_oauth_clients() -> dict[str, dict]:
    clients = _load_json_store(OAUTH_CLIENTS_FILE)
    # Bootstrap from env for local dev (exact redirect URIs, comma-separated)
    bootstrap = os.getenv("OAUTH_REDIRECT_URIS", "").strip()
    if bootstrap and not clients:
        uris = [u.strip() for u in bootstrap.split(",") if u.strip()]
        if uris:
            clients = {
                "mcp-starter-local": {
                    "redirect_uris": uris,
                    "created_at": time.time(),
                }
            }
            try:
                _atomic_write_json(OAUTH_CLIENTS_FILE, clients)
            except OSError:
                pass
    return clients


def _save_oauth_clients(clients: dict[str, dict]) -> None:
    try:
        _atomic_write_json(OAUTH_CLIENTS_FILE, clients)
    except OSError:
        pass


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host
    return "unknown"


def _rate_limit(request: Request, bucket: str) -> bool:
    key = f"{bucket}:{_client_ip(request)}"
    now = time.time()
    window = float(OAUTH_RATE_WINDOW)
    hits = [t for t in _oauth_rate_hits.get(key, []) if now - t < window]
    if len(hits) >= OAUTH_RATE_LIMIT:
        return False
    hits.append(now)
    _oauth_rate_hits[key] = hits
    return True


def _escape_html(value: str) -> str:
    return html.escape(value, quote=True)


def _validate_redirect_uri(redirect_uri: str) -> str | None:
    if not redirect_uri:
        return "redirect_uri is required"
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in ("http", "https"):
        return "redirect_uri must use http or https"
    if not parsed.netloc:
        return "redirect_uri must include a host"
    if parsed.fragment:
        return "redirect_uri must not include a fragment"
    return None


def _lookup_client(client_id: str) -> dict | None:
    if not client_id:
        return None
    return _load_oauth_clients().get(client_id)


def _redirect_uri_allowed(client: dict, redirect_uri: str) -> bool:
    allowed = client.get("redirect_uris") or []
    return redirect_uri in allowed


# ─── In-memory stores ─────────────────────────────────────────────────────────

auth_codes: dict[str, dict] = {}
refresh_tokens: dict[str, dict] = {}
oauth_clients: dict[str, dict] = {}
_sse_queues: dict[str, asyncio.Queue] = {}

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def verify_password(plain: str) -> bool:
    return secrets.compare_digest(plain.encode("utf-8"), OAUTH_PASSWORD.encode("utf-8"))


def create_access_token(sub: str = "user") -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
        "type": "access",
        "aud": BASE_URL,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def create_refresh_token() -> str:
    token = secrets.token_urlsafe(64)
    refresh_tokens[token] = {
        "issued_at": time.time(),
        "expires": time.time() + REFRESH_TOKEN_TTL,
    }
    _save_refresh_tokens(refresh_tokens)
    return token


def verify_access_token(token: str) -> Optional[dict]:
    try:
        return pyjwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=BASE_URL,
            options={"require": ["exp", "aud"]},
        )
    except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
        return None


def pkce_verify(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def get_bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def authenticate_request(request: Request) -> bool:
    token = get_bearer_token(request)
    if not token:
        return False
    if MCP_API_KEY and token == MCP_API_KEY:
        return True
    if verify_access_token(token):
        return True
    return False


def _messages_from_body(body: Any) -> list[dict]:
    if isinstance(body, list):
        return [msg for msg in body if isinstance(msg, dict)]
    if isinstance(body, dict):
        return [body]
    return []


def request_requires_auth(body: Any) -> bool:
    return any(msg.get("method", "") in PROTECTED_METHODS for msg in _messages_from_body(body))


def negotiate_protocol_version(requested: str) -> tuple[str, str | None]:
    if not requested:
        return DEFAULT_PROTOCOL_VERSION, None
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested, None
    supported = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
    return DEFAULT_PROTOCOL_VERSION, f"Unsupported protocol version: {requested}. Supported: {supported}"


def _validate_transport_request(request: Request) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if origin and ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"error": "Forbidden origin"}, status_code=403)
    protocol = request.headers.get("mcp-protocol-version")
    if protocol and protocol not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            {"error": f"Unsupported MCP-Protocol-Version: {protocol}"},
            status_code=400,
        )
    if request.method == "POST":
        accept = request.headers.get("accept", "*/*")
        if accept != "*/*" and "application/json" not in accept and "text/event-stream" not in accept:
            return JSONResponse({"error": "Accept must include application/json"}, status_code=406)
    return None


def _json_log(**payload: Any) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat()}
    record.update(payload)
    _logger.info(json.dumps(record, ensure_ascii=False, sort_keys=True))


def _primary_message(body: Any) -> dict[str, Any]:
    messages = _messages_from_body(body)
    return messages[0] if messages else {}


def _jsonrpc_summary(body: Any) -> dict[str, Any]:
    msg = _primary_message(body)
    params = msg.get("params") or {}
    return {
        "jsonrpc_method": msg.get("method", ""),
        "tool_name": params.get("name", ""),
        "msg_id": msg.get("id"),
        "is_notification": msg.get("id") is None and bool(msg.get("method")),
    }


def _validate_streamable_headers(request: Request, body: Any) -> Optional[dict]:
    """
    Soft-validate MCP HTTP headers without breaking older clients that don't send them yet.
    If a standard header is present, it must match the JSON-RPC body.
    """
    msg = _primary_message(body)
    method = str(msg.get("method", ""))
    params = msg.get("params") or {}

    header_method = request.headers.get("mcp-method")
    if header_method and header_method != method:
        return _err(msg.get("id"), -32001, f"Header mismatch: Mcp-Method '{header_method}' != '{method}'")

    header_name = request.headers.get("mcp-name")
    expected_name = ""
    if method in {"tools/call", "resources/read", "prompts/get"}:
        expected_name = str(params.get("name") or params.get("uri") or "")
    if header_name and header_name != expected_name:
        return _err(msg.get("id"), -32001, f"Header mismatch: Mcp-Name '{header_name}' != '{expected_name}'")

    return None


def _is_notification_message(body: Any) -> bool:
    msg = _primary_message(body)
    return msg.get("id") is None and bool(msg.get("method", ""))


def _response_status_for_empty_result(body: Any) -> int:
    if _is_notification_message(body):
        return 202
    return 204

# ─── OAuth 2.0 PKCE endpoints ─────────────────────────────────────────────────

AUTHORIZE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'">
  <title>MCP Obsidian – Authorize</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      background: #0d0d0d; color: #e2e2e2;
      display: flex; align-items: center; justify-content: center; min-height: 100vh;
    }}
    .card {{
      background: #181818; border: 1px solid #2a2a2a; border-radius: 14px;
      padding: 2rem 2.25rem; width: 100%; max-width: 380px;
    }}
    h1 {{ font-size: 1.2rem; font-weight: 600; color: #fff; margin-bottom: 1.5rem; }}
    label {{ display: block; font-size: 0.8rem; color: #888; margin-bottom: 0.35rem; }}
    input[type=password] {{
      width: 100%; padding: 0.55rem 0.8rem; border-radius: 7px;
      border: 1px solid #333; background: #111; color: #fff;
      font-size: 0.95rem; outline: none; transition: border-color .15s;
    }}
    input[type=password]:focus {{ border-color: #7c6af7; }}
    button {{
      margin-top: 1.2rem; width: 100%; padding: 0.65rem;
      background: #7c6af7; color: #fff; border: none; border-radius: 7px;
      font-size: 0.95rem; font-weight: 500; cursor: pointer; transition: background .15s;
    }}
    button:hover {{ background: #6a58e5; }}
    .err {{ color: #f87171; font-size: 0.8rem; margin-top: 0.6rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Obsidian MCP</h1>
    <form method="POST" action="/authorize">
      <input type="hidden" name="code_challenge"        value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <input type="hidden" name="redirect_uri"          value="{redirect_uri}">
      <input type="hidden" name="client_id"             value="{client_id}">
      <input type="hidden" name="state"                 value="{state}">
      <label for="pw">Password</label>
      <input type="password" id="pw" name="password" autofocus required>
      {error}
      <button type="submit">Authorize</button>
    </form>
  </div>
</body>
</html>"""


def _authorize_form_context(
    *,
    code_challenge: str,
    code_challenge_method: str,
    redirect_uri: str,
    client_id: str,
    state: str,
    error: str = "",
) -> dict[str, str]:
    return {
        "code_challenge": _escape_html(code_challenge),
        "code_challenge_method": _escape_html(code_challenge_method),
        "redirect_uri": _escape_html(redirect_uri),
        "client_id": _escape_html(client_id),
        "state": _escape_html(state),
        "error": error,
    }


def _validate_authorize_params(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
) -> str | None:
    if not state:
        return "state is required"
    if not code_challenge:
        return "code_challenge is required"
    if code_challenge_method != "S256":
        return "Only S256 PKCE is supported"
    uri_err = _validate_redirect_uri(redirect_uri)
    if uri_err:
        return uri_err
    client = _lookup_client(client_id)
    if client is None:
        return "Unknown client_id — register via POST /register first"
    if not _redirect_uri_allowed(client, redirect_uri):
        return "redirect_uri not registered for this client"
    return None


async def authorize_get(request: Request) -> HTMLResponse:
    if not _rate_limit(request, "authorize"):
        return HTMLResponse("Too many requests", status_code=429)
    p = request.query_params
    ctx = _authorize_form_context(
        code_challenge=p.get("code_challenge", ""),
        code_challenge_method=p.get("code_challenge_method", "S256"),
        redirect_uri=p.get("redirect_uri", ""),
        client_id=p.get("client_id", ""),
        state=p.get("state", ""),
    )
    err = _validate_authorize_params(
        p.get("client_id", ""),
        p.get("redirect_uri", ""),
        p.get("code_challenge", ""),
        p.get("code_challenge_method", "S256"),
        p.get("state", ""),
    )
    if err:
        ctx["error"] = f'<p class="err">{_escape_html(err)}</p>'
    return HTMLResponse(
        AUTHORIZE_HTML.format(**ctx),
        headers={"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
    )


async def authorize_post(request: Request) -> Response:
    if not _rate_limit(request, "authorize"):
        return HTMLResponse("Too many requests", status_code=429)
    form = await request.form()
    password              = str(form.get("password", ""))
    code_challenge        = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", "S256"))
    redirect_uri          = str(form.get("redirect_uri", ""))
    client_id             = str(form.get("client_id", ""))
    state                 = str(form.get("state", ""))

    ctx = _authorize_form_context(
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        redirect_uri=redirect_uri,
        client_id=client_id,
        state=state,
    )
    security_headers = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}

    param_err = _validate_authorize_params(
        client_id, redirect_uri, code_challenge, code_challenge_method, state,
    )
    if param_err:
        ctx["error"] = f'<p class="err">{_escape_html(param_err)}</p>'
        return HTMLResponse(AUTHORIZE_HTML.format(**ctx), status_code=400, headers=security_headers)

    if not verify_password(password):
        ctx["error"] = '<p class="err">Invalid password. Please try again.</p>'
        return HTMLResponse(AUTHORIZE_HTML.format(**ctx), status_code=401, headers=security_headers)

    code = secrets.token_urlsafe(32)
    auth_codes[code] = {
        "challenge": code_challenge,
        "method": "S256",
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "state": state,
        "expires": time.time() + 300,
    }

    params = {"code": code, "state": state}
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)


async def token_endpoint(request: Request) -> JSONResponse:
    """Unified token endpoint — handles authorization_code AND refresh_token (RFC 6749)."""
    if not _rate_limit(request, "token"):
        return JSONResponse({"error": "slow_down", "error_description": "Rate limit exceeded"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            return JSONResponse({"error": "invalid_request", "error_description": "Body must be JSON or form-encoded"}, status_code=400)

    grant_type = body.get("grant_type", "")
    resource = body.get("resource", "")
    if resource and resource.rstrip("/") != BASE_URL:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "resource does not match this MCP server"},
            status_code=400,
        )

    # ── Refresh token grant ───────────────────────────────────────────────────
    if grant_type == "refresh_token":
        old_token = body.get("refresh_token", "")
        record = refresh_tokens.pop(old_token, None)
        _save_refresh_tokens(refresh_tokens)
        if not record or time.time() > record["expires"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired refresh token"}, status_code=400)
        new_access  = create_access_token()
        new_refresh = create_refresh_token()
        return JSONResponse({
            "access_token":  new_access,
            "token_type":    "Bearer",
            "expires_in":    ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh,
            "scope":         "mcp",
        })

    # ── Authorization code grant ──────────────────────────────────────────────
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code          = body.get("code", "")
    code_verifier = body.get("code_verifier", "")
    redirect_uri  = body.get("redirect_uri", "")
    client_id     = body.get("client_id", "")

    record = auth_codes.pop(code, None)
    if not record:
        return JSONResponse({"error": "invalid_grant", "error_description": "Unknown or expired code"}, status_code=400)
    if time.time() > record["expires"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)
    if record.get("client_id") != client_id:
        return JSONResponse({"error": "invalid_grant", "error_description": "client_id mismatch"}, status_code=400)
    if record["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)
    client = _lookup_client(client_id)
    if client is None or not _redirect_uri_allowed(client, redirect_uri):
        return JSONResponse({"error": "invalid_grant", "error_description": "Invalid client or redirect_uri"}, status_code=400)
    if not pkce_verify(code_verifier, record["challenge"], record["method"]):
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    access_token  = create_access_token()
    refresh_token = create_refresh_token()

    return JSONResponse({
        "access_token":  access_token,
        "token_type":    "Bearer",
        "expires_in":    ACCESS_TOKEN_TTL,
        "refresh_token": refresh_token,
        "scope":         "mcp",
    })


async def token_refresh_endpoint(request: Request) -> JSONResponse:
    if not _rate_limit(request, "token"):
        return JSONResponse({"error": "slow_down"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    grant_type    = body.get("grant_type", "")
    refresh_token = body.get("refresh_token", "")

    if grant_type != "refresh_token":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    record = refresh_tokens.pop(refresh_token, None)
    _save_refresh_tokens(refresh_tokens)
    if not record or time.time() > record["expires"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired refresh token"}, status_code=400)

    new_access  = create_access_token()
    new_refresh = create_refresh_token()

    return JSONResponse({
        "access_token":  new_access,
        "token_type":    "Bearer",
        "expires_in":    ACCESS_TOKEN_TTL,
        "refresh_token": new_refresh,
        "scope":         "mcp",
    })


async def health_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "obsidian-mcp", "version": SERVER_VERSION})


async def oauth_authorization_server_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/authorize",
        "token_endpoint": f"{BASE_URL}/token",
        "registration_endpoint": f"{BASE_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
        "client_id_metadata_document_supported": False,
    })


async def oauth_protected_resource_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })

# ─── Vault tools ──────────────────────────────────────────────────────────────

def _list_inbox() -> dict:
    inbox = VAULT_PATH / INBOX
    if not inbox.exists():
        return {"error": f"{INBOX}/ folder not found", "files": []}
    files = []
    truncated = False
    for f in sorted(inbox.rglob("*.md")):
        rel = str(f.relative_to(VAULT_PATH)).replace("\\", "/")
        if vsec.is_protected_resolved(VAULT_PATH, f.resolve()):
            continue
        files.append({"path": rel, "name": f.name})
        if len(files) >= LIST_INBOX_MAX:
            truncated = True
            break
    out: dict[str, Any] = {"count": len(files), "files": files}
    if truncated:
        out["truncated"] = True
        out["returned"] = len(files)
        out["hint"] = (
            f"Inbox listing capped at {LIST_INBOX_MAX}. "
            "Use run_skill('vault-find', ['--today', '--brief']) for recent files."
        )
    return out


def _read_note(path: str) -> dict:
    if not path:
        return {"error": "path is required"}
    note, err = vsec.resolve_under_vault(VAULT_PATH, path)
    if err:
        return {"error": err}
    if path.endswith("vault-graph.json") or path.endswith(GRAPH_JSON):
        return {"error": f"{GRAPH_JSON} is large and would flood the context. Use run_skill('vault-graph', ['query', '<subcmd>', ...]) — subcmds: backlinks, forward, neighbors, hubs, orphans, tag, node, path, find, stats."}
    _BLOAT_WARN_PATHS = (
        f"{META}/CHANGELOG.md",
    )
    warn = None
    norm = path.replace("\\", "/")
    if norm in _BLOAT_WARN_PATHS or norm.endswith("/CHANGELOG.md"):
        warn = (
            "Token-heavy file. Prefer vault-graph query stats, vault-dispatch, or vault-find. "
            "Only read if the task explicitly requires this file."
        )
    if path.endswith(".json") or path.endswith(".jsonl") or path.endswith(".ndjson"):
        json_warn = (
            "Structured data file. Do NOT read as plain text — use read_json (schema-first) "
            "or read_jsonl for line-based files. read_note truncates from the start and "
            "loses content at the end."
        )
        warn = f"{warn} {json_warn}" if warn else json_warn
    if not note.exists():
        return {"error": f"Note not found: {path}"}
    try:
        raw = note.read_text(encoding="utf-8")
        content, truncated = _truncate_text(raw, READ_NOTE_MAX_CHARS)
        out: dict[str, Any] = {"path": path, "content": content}
        if truncated:
            out["truncated"] = True
            out["content_chars"] = len(content)
            out["content_chars_total"] = len(raw)
        if warn:
            out["warning"] = warn
        return out
    except OSError as e:
        return {"error": str(e)}


def _read_json(args: dict) -> dict:
    path = args.get("path", "")
    err = _json_data_path_error(path)
    if err:
        return err
    file_path = VAULT_PATH / path
    try:
        fields = args.get("fields")
        if fields is not None and not isinstance(fields, list):
            fields = [str(fields)]
        result = vjr.read_json(
            file_path,
            mode=str(args.get("mode", "schema")),
            query=args.get("query"),
            fields=fields,
            limit=args.get("limit"),
            offset=int(args.get("offset", 0)),
            depth=args.get("depth"),
            max_chars=READ_NOTE_MAX_CHARS,
        )
        if "error" not in result:
            result["path"] = path
        return result
    except Exception as e:
        return {"error": str(e)}


def _read_jsonl(args: dict) -> dict:
    path = args.get("path", "")
    err = _json_data_path_error(path)
    if err:
        return err
    file_path = VAULT_PATH / path
    try:
        fields = args.get("fields")
        if fields is not None and not isinstance(fields, list):
            fields = [str(fields)]
        result = vjr.read_jsonl(
            file_path,
            mode=str(args.get("mode", "rows")),
            offset=int(args.get("offset", 0)),
            limit=args.get("limit"),
            fields=fields,
        )
        if "error" not in result:
            result["path"] = path
        return result
    except Exception as e:
        return {"error": str(e)}


def _mark_graph_stale() -> None:
    """Touch a flag file so the next vault-graph query regenerates before responding.
    Cheap: just creates an empty file. The vault-graph skill checks this flag in main()
    and lazily regenerates the snapshot, then removes the flag."""
    try:
        flag = VAULT_PATH / Path(GRAPH_STALE)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    except OSError:
        pass  # never fail a write because of the staleness flag


def _atomic_write_text(note: Path, content: str) -> None:
    """Write via temp file + rename (atomic on POSIX)."""
    tmp = note.with_name(f".{note.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(note)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _note_path_error(path: str, *, write: bool = False) -> dict | None:
    """Return an error dict if path is invalid; None if OK."""
    resolved, err = vsec.resolve_under_vault(VAULT_PATH, path)
    if err:
        if write and "_PRIVADO" in err:
            return {"error": "Access denied: _PRIVADO/ is write-protected via MCP."}
        return {"error": err}
    if path.endswith("vault-graph.json") or path.endswith(GRAPH_JSON):
        return {
            "error": (
                "vault-graph.json is large. Use run_skill('vault-graph', "
                "['query', '<subcmd>', ...]) instead."
            )
        }
    return None


def _json_data_path_error(path: str) -> dict | None:
    """Return an error dict if path is invalid for read_json/read_jsonl; None if OK."""
    _, err = vsec.resolve_under_vault(VAULT_PATH, path)
    if err:
        return {"error": err}
    if path.endswith("vault-graph.json") or path.endswith(GRAPH_JSON):
        return {
            "error": (
                "vault-graph.json is large. Use run_skill('vault-graph', "
                "['query', '<subcmd>', ...]) instead."
            )
        }
    return None


def _parse_yaml_scalar(value: str) -> Any:
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "~"):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    try:
        return int(value) if "." not in value else float(value)
    except ValueError:
        return value


def _parse_frontmatter_yaml(raw: str) -> tuple[dict[str, Any], str | None]:
    """Minimal YAML subset for Obsidian frontmatter (no external deps)."""
    data: dict[str, Any] = {}
    warning: str | None = None
    for line_no, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            if warning is None:
                warning = f"line {line_no}: skipped non key-value line"
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            continue
        try:
            data[key] = _parse_yaml_scalar(value.strip())
        except ValueError as exc:
            if warning is None:
                warning = f"line {line_no}: {exc}"
    return data, warning


def _extract_frontmatter(text: str) -> tuple[dict[str, Any], str | None]:
    if not text.startswith("---"):
        return {}, "No frontmatter block (file does not start with ---)"
    lines = text.splitlines()
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            block = "\n".join(lines[1:idx])
            fm, warn = _parse_frontmatter_yaml(block)
            return fm, warn
    return {}, "Unclosed frontmatter block (missing closing ---)"


def _write_note(path: str, content: str, mode: str = "upsert") -> dict:
    """Unified write. mode: create|update|append|upsert (default: upsert)."""
    note, err = vsec.resolve_under_vault(VAULT_PATH, path)
    if err:
        if "_PRIVADO" in err:
            return {"error": "Access denied: _PRIVADO/ is write-protected via MCP."}
        return {"error": err}
    if mode == "create":
        note.parent.mkdir(parents=True, exist_ok=True)
        if note.exists():
            return {"error": f"Note already exists: {path}. Use mode='upsert' to overwrite."}
        note.write_text(content, encoding="utf-8")
        _mark_graph_stale()
        return {"created": path}
    elif mode in ("update", "upsert"):
        if mode == "update" and not note.exists():
            return {"error": f"Note not found: {path}. Use mode='create' or 'upsert'."}
        existed = note.exists()
        note.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(note, content)
        _mark_graph_stale()
        result: dict[str, Any] = {"updated": path}
        if mode == "upsert" and existed:
            result["warning"] = (
                "File already existed. Prefer edit_note for surgical edits to save tokens; "
                "use mode='update' when intentionally overwriting the full note."
            )
        return result
    elif mode == "append":
        if not note.exists():
            return {"error": f"Note not found: {path}. Use mode='upsert' to create first."}
        existing = note.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n") else "\n"
        note.write_text(existing + sep + content, encoding="utf-8")
        _mark_graph_stale()
        return {"appended_to": path, "appended_chars": len(content)}
    else:
        return {"error": f"Unknown mode: '{mode}'. Valid: create, update, append, upsert."}


def _read_frontmatter(path: str) -> dict:
    err = _note_path_error(path)
    if err:
        return err
    note = VAULT_PATH / path
    if not note.exists():
        return {"error": f"Note not found: {path}"}
    try:
        fm, warn = _extract_frontmatter(note.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"error": str(exc)}
    out: dict[str, Any] = {"path": path, "frontmatter": fm}
    if warn:
        out["warning"] = warn
    return out


_BULK_READ_MAX = 20


def _bulk_read(paths: list[str]) -> dict:
    if not paths:
        return {"error": "paths is required (non-empty list)"}
    if len(paths) > _BULK_READ_MAX:
        return {"error": f"At most {_BULK_READ_MAX} paths per call (got {len(paths)})"}
    notes: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for path in paths:
        if not isinstance(path, str) or not path:
            errors[str(path)] = "invalid path"
            continue
        err = _note_path_error(path)
        if err:
            errors[path] = err["error"]
            continue
        note = VAULT_PATH / path
        if not note.exists():
            errors[path] = f"Note not found: {path}"
            continue
        try:
            raw = note.read_text(encoding="utf-8")
            content, truncated = _truncate_text(raw, READ_NOTE_MAX_CHARS)
            if truncated:
                notes[path] = {
                    "content": content,
                    "truncated": True,
                    "content_chars": len(content),
                    "content_chars_total": len(raw),
                }
            else:
                notes[path] = content
        except OSError as exc:
            errors[path] = str(exc)
    out: dict[str, Any] = {"count": len(notes), "notes": notes}
    if errors:
        out["errors"] = errors
    return out


def _edit_note(
    path: str,
    old_str: str,
    new_str: str,
    expect_count: int = 1,
    replace_all: bool = False,
) -> dict:
    """Surgical string replacement with expect_count / replace_all."""
    err = _note_path_error(path, write=True)
    if err:
        return err
    note = VAULT_PATH / path
    if not note.exists():
        return {"error": f"Note not found: {path}. Use write_note mode='create' first."}
    if not old_str:
        return {"error": "old_str is required"}
    if expect_count < 1:
        return {"error": "expect_count must be >= 1"}
    content = note.read_text(encoding="utf-8")
    count = content.count(old_str)
    if count == 0:
        return {"error": f"old_str not found in {path}. Check exact whitespace and newlines."}
    if replace_all:
        new_content = content.replace(old_str, new_str)
        replacements = count
    elif count != expect_count:
        return {
            "error": (
                f"old_str found {count} times in {path}, expected {expect_count}. "
                "Add context lines, set expect_count, or use replace_all=true."
            ),
            "found_count": count,
            "expect_count": expect_count,
        }
    elif expect_count == 1:
        new_content = content.replace(old_str, new_str, 1)
        replacements = 1
    else:
        parts = content.split(old_str)
        new_content = new_str.join(parts)
        replacements = expect_count
    _atomic_write_text(note, new_content)
    _mark_graph_stale()
    return {"edited": path, "replacements": replacements}


def _move_note(source: str, destination: str, overwrite: bool = False) -> dict:
    if not source or not destination:
        return {"error": "source and destination are required"}
    for path in (source, destination):
        err = _note_path_error(path, write=True)
        if err:
            return err
    src, src_err = vsec.resolve_under_vault(VAULT_PATH, source)
    dst, dst_err = vsec.resolve_under_vault(VAULT_PATH, destination)
    if src_err:
        return {"error": src_err}
    if dst_err:
        return {"error": dst_err}
    if not src.exists():
        return {"error": f"Source not found: {source}"}
    if not src.is_file():
        return {"error": f"Source is not a file: {source}"}
    if dst.exists():
        if not overwrite:
            return {
                "error": (
                    f"Destination already exists: {destination}. "
                    "Pass overwrite=true to replace."
                )
            }
        if dst.is_dir():
            return {"error": f"Cannot overwrite directory: {destination}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    _mark_graph_stale()
    return {"moved": {"from": source, "to": destination}}


def _list_folder(path: str = "", stats: bool = False, recursive: bool = False) -> dict:
    folder = (VAULT_PATH / path).resolve() if path else VAULT_PATH.resolve()
    try:
        folder.relative_to(VAULT_PATH.resolve())
    except ValueError:
        return {"error": "Path traversal not allowed"}
    if vsec.is_protected_resolved(VAULT_PATH, folder):
        return {"error": "Access denied: _PRIVADO/ contents are never exposed via MCP."}
    if not folder.exists():
        return {"error": f"Folder not found: {path}"}
    if recursive:
        files = []
        truncated = False
        for f in sorted(folder.rglob("*.md")):
            if f.name.startswith(".") or vsec.is_protected_resolved(VAULT_PATH, f.resolve()):
                continue
            files.append({
                "path": str(f.relative_to(VAULT_PATH)).replace("\\", "/"),
                "name": f.name,
            })
            if len(files) >= LIST_RECURSIVE_MAX:
                truncated = True
                break
        out: dict[str, Any] = {"path": path or "/", "files": files, "count": len(files)}
        if truncated:
            out["truncated"] = True
            out["returned"] = len(files)
            out["hint"] = (
                f"Recursive listing capped at {LIST_RECURSIVE_MAX}. "
                "Narrow path or use vault-find / vault-dispatch."
            )
        return out
    items = []
    for p in folder.iterdir():
        if p.name.startswith(".") or p.name == "_PRIVADO":
            continue
        item: dict = {"name": p.name, "type": "folder" if p.is_dir() else "file"}
        if p.is_file():
            item["ext"] = p.suffix.lower()
            if stats:
                s = p.stat()
                item["mtime"] = datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
                item["ctime"] = datetime.fromtimestamp(s.st_ctime).strftime("%Y-%m-%dT%H:%M:%S")
                item["size_bytes"] = s.st_size
        items.append(item)
    items.sort(key=lambda x: (x["type"] == "file", x["name"]))
    return {"path": path or "/", "items": items, "count": len(items)}


def _find_files(args: dict) -> dict:
    limit = args.get("limit")
    if limit is None:
        limit = FIND_FILES_MAX
    else:
        try:
            limit = max(1, min(int(limit), FIND_FILES_MAX))
        except (TypeError, ValueError):
            limit = FIND_FILES_MAX

    params = vfs.FindFilesParams(
        name=(args.get("name") or None),
        ext=(args.get("ext") or None),
        file_type=(args.get("type") or None),
        path_contains=(args.get("path_contains") or None),
        modified_after=(args.get("modified_after") or None),
        modified_before=(args.get("modified_before") or None),
        created_after=(args.get("created_after") or None),
        created_before=(args.get("created_before") or None),
        today=bool(args.get("today", False)),
        yesterday=bool(args.get("yesterday", False)),
        min_size=args.get("min_size"),
        max_size=args.get("max_size"),
        sort_by=(args.get("sort_by") or None),
        limit=limit,
        exclude_md=bool(args.get("exclude_md", False)),
    )
    if not any([
        params.name, params.ext, params.file_type, params.path_contains,
        params.modified_after, params.modified_before,
        params.created_after, params.created_before,
        params.today, params.yesterday,
        params.min_size is not None, params.max_size is not None,
    ]):
        return {
            "error": "At least one filter required (name, ext, type, path_contains, date, or size).",
            "files": [],
            "count": 0,
            "hint": (
                "Examples: ext='.pdf', type='docs', name='*contrato*', "
                "path_contains='castelform', today=true"
            ),
        }

    result = vfs.search_files(VAULT_PATH, params)
    if result.get("files"):
        result["hint_read"] = (
            "Metadata only. To read content: run_skill with the right adapter "
            "(e.g. dre-parser for legislation PDFs, file-reader when available)."
        )
    return result


def _normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _search_notes(query: str) -> dict:
    q = (query or "").strip()
    if not q:
        return {"error": "query is required", "results": []}
    if len(q) < SEARCH_MIN_QUERY_LEN:
        return {
            "error": f"query too short (min {SEARCH_MIN_QUERY_LEN} chars)",
            "results": [],
            "hint": "Use a specific keyword or run_skill('vault-dispatch', ['<task>']).",
        }
    if _SEARCH_IPV4_RE.match(q):
        return {
            "error": "IPv4 queries are blocked in search_notes (too many false positives).",
            "results": [],
            "count": 0,
            "hint": (
                "Use read_note with a known path, run_skill('vault-find', ...), "
                "or ask Jorge for the note location."
            ),
        }
    results = []
    terms = [_normalize(t) for t in q.split() if t]
    deadline = time.monotonic() + SEARCH_TIMEOUT_SEC
    hit_cap = False
    for note in VAULT_PATH.rglob("*.md"):
        if time.monotonic() > deadline:
            return {
                "query": q,
                "results": results,
                "returned": len(results),
                "truncated": True,
                "note": f"Search stopped at {SEARCH_TIMEOUT_SEC:.0f}s limit",
                "hint": "Refine query or use run_skill('vault-search', ...) / vault-find.",
            }
        rel = str(note.relative_to(VAULT_PATH))
        if vsec.is_protected_resolved(VAULT_PATH, note.resolve()):
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            continue
        norm_text = _normalize(text + " " + note.name)
        if all(t in norm_text for t in terms):
            results.append({
                "path": rel.replace("\\", "/"),
                "name": note.name,
            })
            if len(results) >= SEARCH_MAX_RESULTS:
                hit_cap = True
                break
    out: dict[str, Any] = {
        "query": q,
        "results": results,
        "returned": len(results),
    }
    if hit_cap:
        out["truncated"] = True
        out["hint"] = (
            f"Results capped at {SEARCH_MAX_RESULTS}. Refine query; "
            "for recent files use vault-find; for advanced text use vault-search skill."
        )
    return out


def _get_current_datetime() -> dict:
    now = datetime.now(ZoneInfo("Europe/Lisbon"))
    utc = datetime.now(timezone.utc)
    return {
        "datetime_local": now.isoformat(),
        "datetime_utc": utc.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "weekday": now.strftime("%A"),
        "iso_week": now.isocalendar()[1],
        "simplenote_folder": now.strftime("%Y/%m"),
    }


def _skill_subprocess_env() -> dict[str, str]:
    allowed = ("PATH", "LANG", "LC_ALL", "HOME", "PYTHONIOENCODING", "TZ")
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env["VAULT_PATH"] = str(VAULT_PATH)
    env["SKILLS_ROOT"] = str(SKILLS_ROOT)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run_skill(skill: str, argv: Optional[list] = None) -> dict:
    if not skill:
        return {"error": "skill name is required"}
    if "/" in skill or ".." in skill:
        return {"error": "Invalid skill name"}
    # Circuit breaker: bloqueia execução de qualquer skill se existir AGENT_PAUSE
    pause_flag = VAULT_PATH / AGENT_PAUSE
    if pause_flag.exists():
        try:
            reason = pause_flag.read_text(encoding="utf-8").strip() or "sem motivo especificado"
        except Exception:
            reason = "sem motivo especificado"
        return {
            "skill": skill,
            "returncode": 0,
            "stdout": f"[CIRCUIT BREAKER] Execução bloqueada: {reason}",
            "stderr": "",
            "blocked": True,
        }
    skills_root = SKILLS_ROOT
    matches = list(skills_root.rglob(f"{skill}/main.py"))
    if not matches:
        return {"error": f"Skill '{skill}' not found under {skills_root}/"}
    if len(matches) > 1:
        return {"error": f"Ambiguous skill name '{skill}' — multiple matches under {skills_root}/"}
    skill_path = matches[0]
    skill_dir = skill_path.parent
    if sys.platform == "win32":
        venv_py = skill_dir / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = skill_dir / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    cmd = [py, str(skill_path)] + [str(a) for a in (argv or [])]
    env = _skill_subprocess_env()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(skill_path.parent),
            env=env,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=45)
        stdout = stdout or ""
        stderr = stderr or ""
        stdout, stdout_trunc = _truncate_text(stdout, RUN_SKILL_STDOUT_MAX, _SKILL_TRUNCATE_SUFFIX)
        stderr, stderr_trunc = _truncate_text(stderr, RUN_SKILL_STDOUT_MAX, _SKILL_TRUNCATE_SUFFIX)
        out: dict[str, Any] = {
            "skill": skill,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        if stdout_trunc:
            out["stdout_truncated"] = True
        if stderr_trunc:
            out["stderr_truncated"] = True
        return out
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                proc.kill()
        return {"error": f"Skill '{skill}' timed out after 45s"}
    except Exception as e:
        return {"error": str(e)}


def _dispatch_typed_skill(tool_name: str, args: dict) -> Any:
    manifest = SKILL_MANIFEST_BY_NAME.get(tool_name)
    if manifest is None:
        return {"error": f"Unknown typed skill tool: {tool_name}"}
    try:
        argv = skill_registry.args_to_argv(manifest, args or {})
    except ValueError as exc:
        return {"error": str(exc)}
    out = _run_skill(manifest.skill_dir, argv)
    if out.get("error"):
        return out
    stdout = (out.get("stdout") or "").strip()
    if stdout:
        try:
            out["result"] = json.loads(stdout)
        except json.JSONDecodeError:
            out["result"] = stdout
    return out


def _session_start() -> dict:
    """Bootstrap tool: returns minimal vault router (_README.router.md) + datetime."""
    result: dict[str, Any] = {
        "datetime": _get_current_datetime(),
        "note": (
            "Call session_start ONCE per conversation — do not repeat. "
            "Reuse datetime below — do NOT call get_current_datetime or get_current_timestamp."
        ),
    }
    router = VAULT_PATH / "_README.router.md"
    fallback = VAULT_PATH / "_README.md"
    if router.exists():
        result["readme"] = router.read_text(encoding="utf-8")
        result["router_source"] = "_README.router.md"
    elif fallback.exists():
        result["readme"] = fallback.read_text(encoding="utf-8")
        result["router_source"] = "_README.md"
    else:
        result["readme_error"] = "_README.router.md and _README.md not found"
    return result


def dispatch_tool(name: str, args: dict) -> Any:
    if name in SKILL_MANIFEST_BY_NAME:
        return _dispatch_typed_skill(name, args)
    dispatch = {
        "session_start":        lambda: _session_start(),
        "list_folder":          lambda: _list_folder(args.get("path", ""), bool(args.get("stats", False)), bool(args.get("recursive", False))),
        "find_files":           lambda: _find_files(args),
        "read_note":            lambda: _read_note(args.get("path", "")),
        "read_frontmatter":     lambda: _read_frontmatter(args.get("path", "")),
        "bulk_read":            lambda: _bulk_read(args.get("paths") or []),
        "read_json":            lambda: _read_json(args),
        "read_jsonl":           lambda: _read_jsonl(args),
        "write_note":           lambda: _write_note(args.get("path", ""), args.get("content", ""), args.get("mode", "upsert")),
        "edit_note":            lambda: _edit_note(
            args.get("path", ""),
            args.get("old_str", ""),
            args.get("new_str", ""),
            int(args.get("expect_count", 1)),
            bool(args.get("replace_all", False)),
        ),
        "move_note":            lambda: _move_note(
            args.get("source", ""),
            args.get("destination", ""),
            bool(args.get("overwrite", False)),
        ),

        "search_notes":         lambda: _search_notes(args.get("query", "")),
        "get_current_datetime": lambda: _get_current_datetime(),
        "run_skill":            lambda: _run_skill(args.get("skill", ""), args.get("argv")),
    }
    fn = dispatch.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    return fn()

# ─── MCP tool schema ──────────────────────────────────────────────────────────

BASE_MCP_TOOLS = [
    {
        "name": "session_start",
        "title": "Session Start",
        "description": (
            "Call ONCE at conversation start — never repeat in the same chat. "
            "Returns slim router (_README.router.md ~400 tok) + datetime (date, simplenote_folder, weekday). "
            "Reuse response.datetime — do NOT call get_current_datetime or get_current_timestamp after this. "
            "NO CONTEXT tables or skill catalogs. For routing: run_skill('vault-dispatch', ['<query>'])."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "list_folder",
        "title": "List Folder",
        "description": (
            "List files and subfolders in any vault folder. "
            "path='': vault root. stats=true: mtime/ctime/size. "
            "Non-recursive: all entry types with ext on files (.pdf, .py, …). "
            "recursive=true: all .md under path (capped ~200; use find_files for PDFs/binaries). "
            f"Avoid recursive on {INBOX}/ — token-heavy."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":      {"type": "string", "description": "Vault-relative folder path. Empty for vault root."},
                "stats":     {"type": "boolean", "description": "Include mtime, ctime, size_bytes per file."},
                "recursive": {"type": "boolean", "description": "Return all .md files recursively under path."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_files",
        "title": "Find Files",
        "description": (
            f"Discover vault files by name, extension, type, path, date, or size — metadata only, no content. "
            f"Use for PDFs, docx, xlsx, code, media (.md included unless exclude_md=true). "
            f"Max {FIND_FILES_MAX} results; requires at least one filter. "
            "After discovery, use run_skill to read (dre-parser, vault-find --brief, domain skills). "
            "Types: ebooks, docs, images, audio, video, code, data, archives."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":             {"type": "string", "description": "Glob on filename (e.g. *kleon*, contrato.pdf)"},
                "ext":              {"type": "string", "description": "Comma-separated extensions (e.g. .pdf,.docx)"},
                "type":             {"type": "string", "enum": ["ebooks", "docs", "images", "audio", "video", "code", "data", "archives"]},
                "path_contains":    {"type": "string", "description": "Substring in vault-relative path"},
                "modified_after":   {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"},
                "modified_before":  {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"},
                "created_after":    {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"},
                "created_before":   {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM"},
                "today":            {"type": "boolean", "description": "Files modified or created today"},
                "yesterday":        {"type": "boolean", "description": "Files from yesterday"},
                "min_size":         {"type": "integer", "description": "Minimum size in bytes"},
                "max_size":         {"type": "integer", "description": "Maximum size in bytes"},
                "sort_by":          {"type": "string", "enum": ["modified", "created", "name", "size"]},
                "limit":            {"type": "integer", "description": f"Max results (default/cap {FIND_FILES_MAX})"},
                "exclude_md":       {"type": "boolean", "description": "Omit .md from results"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_note",
        "title": "Read Note",
        "description": (
            "Return note content by vault-relative path. "
            f"Body truncated above ~{READ_NOTE_MAX_CHARS} chars (truncated flag). "
            "Prefer read_frontmatter for YAML only; bulk_read for ≤20 paths."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path (e.g. meta/PROFILE.md)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_frontmatter",
        "title": "Read Frontmatter",
        "description": (
            "Return only the parsed YAML frontmatter of a note (no body). "
            "Use before run_skill to check has_learnings (~200 chars vs full read_note). "
            "Malformed YAML returns partial dict plus warning, not a hard error."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path (e.g. work/projects/my-app/AGENT.md)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bulk_read",
        "title": "Bulk Read Notes",
        "description": (
            "Read up to 20 notes in one round-trip (per-note body cap like read_note). "
            "Returns notes dict plus optional errors (batch does not abort on single failure). "
            "Honours _PRIVADO/ and vault-graph.json blocks."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Vault-relative paths (max 20)",
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_json",
        "title": "Read JSON",
        "description": (
            "Navigate structured .json files schema-first — never use read_note or file-reader on JSON. "
            "Workflow: mode=schema (default) to see shape/types/sizes cheaply, then mode=list to browse "
            "arrays/objects with pagination, then mode=get with query for the exact value. "
            f"query syntax: dotted keys and brackets (e.g. conversations[0].messages[-1].content). "
            f"get values capped at ~{READ_NOTE_MAX_CHARS} chars. "
            "For vault-graph.json use run_skill('vault-graph', ['query', ...]) instead."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "Vault-relative .json path"},
                "mode":   {"type": "string", "enum": ["schema", "get", "list"], "default": "schema"},
                "query":  {"type": "string", "description": "JSON path (e.g. conversations[0].messages). Empty = root."},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For mode=list: project only these keys per entry",
                },
                "limit":  {"type": "integer", "description": "For mode=list: page size (default 20, max 50)"},
                "offset": {"type": "integer", "description": "For mode=list: start index (default 0)"},
                "depth":  {"type": "integer", "description": "For mode=schema: nesting depth (default 2, max 4)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_jsonl",
        "title": "Read JSONL",
        "description": (
            "Read .jsonl/.ndjson files line-by-line without loading the whole file. "
            "mode=schema infers shape from first lines; mode=rows (default) returns a paginated window. "
            "Use offset/next_offset to paginate. Never use read_note on JSONL."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "Vault-relative .jsonl or .ndjson path"},
                "mode":   {"type": "string", "enum": ["rows", "schema"], "default": "rows"},
                "offset": {"type": "integer", "description": "Line offset for mode=rows (default 0)"},
                "limit":  {"type": "integer", "description": "Max lines per page (default 20, max 50)"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project only these keys from each JSON object line",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_note",
        "title": "Write Note",
        "description": (
            "Write a vault note. mode controls behavior: "
            "'upsert' (default) = create or overwrite; "
            "'create' = fail if exists; "
            "'update' = fail if not exists; "
            "'append' = add to end of existing note."
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Vault-relative destination path"},
                "content": {"type": "string", "description": "Markdown content to write"},
                "mode":    {"type": "string", "enum": ["upsert", "create", "update", "append"], "default": "upsert"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_note",
        "title": "Edit Note",
        "description": (
            "Surgically replace an exact string in a note. "
            "Default: old_str must appear exactly expect_count times (default 1). "
            "replace_all=true replaces every occurrence. "
            "Saves ~90% tokens vs read_note + write_note for targeted changes."
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":         {"type": "string", "description": "Vault-relative path of the note to edit"},
                "old_str":      {"type": "string", "description": "Exact literal text to replace"},
                "new_str":      {"type": "string", "description": "Replacement text"},
                "expect_count": {"type": "integer", "description": "Required occurrence count (default 1)", "default": 1, "minimum": 1},
                "replace_all":  {"type": "boolean", "description": "Replace all occurrences (ignores expect_count)", "default": False},
            },
            "required": ["path", "old_str", "new_str"],
            "additionalProperties": False,
        },
    },
    {
        "name": "move_note",
        "title": "Move Note",
        "description": "Move or rename a note within the vault. Fails if destination exists unless overwrite=true.",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "source":      {"type": "string", "description": "Current vault-relative path"},
                "destination": {"type": "string", "description": "Target vault-relative path"},
                "overwrite":   {"type": "boolean", "description": "Replace destination if it exists (default false)"},
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_notes",
        "title": "Search Notes",
        "description": (
            f"LAST RESORT full-text search (case-insensitive AND). Max {SEARCH_MAX_RESULTS} paths per call; "
            "IPv4 queries rejected. "
            "Prefer: vault-dispatch (routing), vault-find (recent files), vault-graph (relations), "
            "vault-search skill (regex/acentos), activity-log (PC context). "
            "Never search vague terms or IPs."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive search string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_current_datetime",
        "title": "Get Current Datetime",
        "description": (
            "Mid-conversation ONLY — never at session bootstrap. "
            "session_start already returns the same datetime; reuse it instead of calling this. "
            "Use only if the chat is long and you need a fresh clock for hoje/ontem/esta semana. "
            "Returns date, time, weekday, ISO week, simplenote_folder (YYYY/MM)."
        ),
        "annotations": {"readOnlyHint": True, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "run_skill",
        "title": "Run Skill",
        "description": (
            "Execute an extension skill by directory name (must have main.py). "
            "Prefer typed tools vault_dispatch, vault_find, vault_graph when available."
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Skill name (e.g. 'vault-dispatch', 'vault-find', 'vault-graph')"},
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CLI arguments to pass (e.g. ['--days', '7', '--dry-run']). Read AGENT.md for valid args.",
                },
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
    },
]


def get_mcp_tools() -> list[dict[str, Any]]:
    manifests = SKILL_MANIFESTS or skill_registry.discover_manifests(find_repo_root() / "skills")
    typed = [skill_registry.manifest_to_mcp_tool(m) for m in manifests]
    return BASE_MCP_TOOLS + typed


MCP_TOOLS = get_mcp_tools()  # backwards-compatible export


# ─── System prompt loader ─────────────────────────────────────────────────────

_FALLBACK_PROMPT = (
    "Obsidian vault MCP server. "
    "For routing: run_skill('vault-dispatch', ['<query>']). "
    "For file discovery: run_skill('vault-find', ...) or find_files. "
    "Hot notes are immutable. _PRIVADO/ is blind. Confirm before deleting."
)


def _load_system_prompt() -> str:
    """Read system prompt from vault file. Falls back to hardcoded minimum if file missing."""
    system_prompt_path = find_repo_root() / "system_prompt.md"
    try:
        text = system_prompt_path.read_text(encoding="utf-8")
        # Extract content inside ```...``` fenced block (the actual prompt)
        import re
        m = re.search(r"```\n(.+?)\n```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # If no fenced block, strip frontmatter and use body
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else text.strip()
        return body
    except Exception:
        return _FALLBACK_PROMPT

# ─── JSON-RPC / MCP message handler ──────────────────────────────────────────

def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_mcp_message(body: Any, authenticated: bool = False) -> dict:
    if not isinstance(body, dict):
        return _err(None, -32600, "Invalid Request: message must be a JSON object")
    method = body.get("method", "")
    msg_id = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        requested = str(params.get("protocolVersion", ""))
        protocol_version, protocol_error = negotiate_protocol_version(requested)
        if protocol_error and requested:
            return _err(msg_id, -32602, protocol_error)
        return _ok(msg_id, {
            "protocolVersion": protocol_version,
            "serverInfo": {"name": "obsidian-mcp", "version": SERVER_VERSION},
            "capabilities": {"tools": {}},
            "instructions": (
                _load_system_prompt()
                if authenticated
                else "Authenticate to receive full instructions."
            ),
        })

    if method == "tools/list":
        return _ok(msg_id, {"tools": copy.deepcopy(get_mcp_tools())})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        result = dispatch_tool(tool_name, tool_args)
        _log_tool_call(tool_name, result)
        payload = {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        }
        if isinstance(result, dict):
            payload["structuredContent"] = result
            if "error" in result:
                payload["isError"] = True
        return _ok(msg_id, payload)

    if method == "ping":
        return _ok(msg_id, {})

    if method == "notifications/initialized":
        return {}

    if msg_id is None:
        return {}

    return _err(msg_id, -32601, f"Method not found: {method}")

# ─── SSE & Messages endpoints ─────────────────────────────────────────────────

def _www_auth() -> str:
    base = globals().get("BASE_URL") or os.getenv("MCP_BASE_URL", "https://your-domain.com").rstrip("/")
    return f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'


async def sse_endpoint(request: Request) -> Response:
    if not authenticate_request(request):
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": _www_auth()},
        )

    session_id = secrets.token_hex(10)
    queue: asyncio.Queue = asyncio.Queue()
    _sse_queues[session_id] = queue

    async def generator() -> AsyncGenerator:
        yield {"event": "endpoint", "data": f"/messages?session_id={session_id}"}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    if msg:
                        yield {"event": "message", "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            _sse_queues.pop(session_id, None)

    return EventSourceResponse(
        generator(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


async def messages_endpoint(request: Request) -> JSONResponse:
    request_id = secrets.token_hex(8)
    transport_error = _validate_transport_request(request)
    if transport_error is not None:
        return transport_error
    try:
        body = await request.json()
    except Exception as exc:
        _json_log(
            event="mcp_legacy_http",
            request_id=request_id,
            http_path="/messages",
            http_status=400,
            status="invalid_json",
            error=str(exc),
        )
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Body must be valid JSON"},
            status_code=400,
        )

    if isinstance(body, list):
        return JSONResponse(
            _err(None, -32600, "Batch requests are not supported"),
            status_code=400,
        )

    summary = _jsonrpc_summary(body)
    if request_requires_auth(body) and not authenticate_request(request):
        _json_log(
            event="mcp_legacy_http",
            request_id=request_id,
            http_path="/messages",
            http_status=401,
            status="unauthorized",
            auth_mode="none",
            mcp_session_id_received=request.query_params.get("session_id", ""),
            **summary,
        )
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": _www_auth()},
        )

    authenticated = authenticate_request(request)
    response = handle_mcp_message(body, authenticated=authenticated)

    session_id = request.query_params.get("session_id", "")
    queue = _sse_queues.get(session_id)

    if queue and response:
        await queue.put(response)
        _json_log(
            event="mcp_legacy_http",
            request_id=request_id,
            http_path="/messages",
            http_status=202,
            status="accepted",
            auth_mode="bearer" if get_bearer_token(request) else "none",
            mcp_session_id_received=session_id,
            **summary,
        )
        return JSONResponse({"status": "accepted"}, status_code=202)

    if response:
        _json_log(
            event="mcp_legacy_http",
            request_id=request_id,
            http_path="/messages",
            http_status=200,
            status="ok",
            auth_mode="bearer" if get_bearer_token(request) else "none",
            mcp_session_id_received=session_id,
            protocol_version_responded=((response.get("result") or {}).get("protocolVersion", "")),
            **summary,
        )
        return JSONResponse(response)
    empty_status = _response_status_for_empty_result(body)
    _json_log(
        event="mcp_legacy_http",
        request_id=request_id,
        http_path="/messages",
        http_status=empty_status,
        status="accepted_notification" if empty_status == 202 else "no_content",
        auth_mode="bearer" if get_bearer_token(request) else "none",
        mcp_session_id_received=session_id,
        **summary,
    )
    return JSONResponse({}, status_code=empty_status)


async def mcp_streamable_endpoint(request: Request) -> Response:
    """
    Streamable HTTP MCP transport (MCP spec 2025-03-26).
    Compatible with: ChatGPT remote MCP, Perplexity MCP, Claude Pro.
    Single endpoint — stateless JSON-RPC, no SSE session required.
    """
    request_id = secrets.token_hex(8)
    transport_error = _validate_transport_request(request)
    if transport_error is not None:
        return transport_error
    if request.method == "GET":
        _json_log(
            event="mcp_streamable_http",
            request_id=request_id,
            http_path="/mcp",
            http_status=405,
            status="method_not_allowed",
            allow="POST",
        )
        return Response(status_code=405, headers={"Allow": "POST"})

    try:
        body = await request.json()
    except Exception as exc:
        _json_log(
            event="mcp_streamable_http",
            request_id=request_id,
            http_path="/mcp",
            http_status=400,
            status="invalid_json",
            error=str(exc),
        )
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Body must be valid JSON"},
            status_code=400,
        )

    if isinstance(body, list):
        _json_log(
            event="mcp_streamable_http",
            request_id=request_id,
            http_path="/mcp",
            http_status=400,
            status="batch_not_supported",
        )
        return JSONResponse(_err(None, -32600, "Batch requests are not supported"), status_code=400)

    summary = _jsonrpc_summary(body)
    header_error = _validate_streamable_headers(request, body)
    if header_error is not None:
        _json_log(
            event="mcp_streamable_http",
            request_id=request_id,
            http_path="/mcp",
            http_status=400,
            status="header_mismatch",
            auth_mode="bearer" if get_bearer_token(request) else "none",
            protocol_version_header=request.headers.get("mcp-protocol-version", ""),
            mcp_session_id_received=request.headers.get("mcp-session-id", ""),
            **summary,
        )
        return JSONResponse(header_error, status_code=400)

    if request_requires_auth(body) and not authenticate_request(request):
        _json_log(
            event="mcp_streamable_http",
            request_id=request_id,
            http_path="/mcp",
            http_status=401,
            status="unauthorized",
            auth_mode="none",
            protocol_version_header=request.headers.get("mcp-protocol-version", ""),
            mcp_session_id_received=request.headers.get("mcp-session-id", ""),
            **summary,
        )
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": _www_auth()},
        )

    if not isinstance(body, dict):
        return JSONResponse(_err(None, -32600, "Invalid Request"), status_code=400)

    authenticated = authenticate_request(request)
    result = handle_mcp_message(body, authenticated=authenticated)
    if not result:
        empty_status = _response_status_for_empty_result(body)
        _json_log(
            event="mcp_streamable_http",
            request_id=request_id,
            http_path="/mcp",
            http_status=empty_status,
            status="accepted_notification" if empty_status == 202 else "no_content",
            auth_mode="bearer" if get_bearer_token(request) else "none",
            protocol_version_header=request.headers.get("mcp-protocol-version", ""),
            mcp_session_id_received=request.headers.get("mcp-session-id", ""),
            **summary,
        )
        return Response(status_code=empty_status)

    _json_log(
        event="mcp_streamable_http",
        request_id=request_id,
        http_path="/mcp",
        http_status=200,
        status="ok",
        auth_mode="bearer" if get_bearer_token(request) else "none",
        protocol_version_header=request.headers.get("mcp-protocol-version", ""),
        protocol_version_responded=((result.get("result") or {}).get("protocolVersion", "") if isinstance(result, dict) else ""),
        mcp_session_id_received=request.headers.get("mcp-session-id", ""),
        **summary,
    )
    return JSONResponse(result)

# ─── Starlette app ────────────────────────────────────────────────────────────

async def authorize_router(request: Request) -> Response:
    if request.method == "GET":
        return await authorize_get(request)
    return await authorize_post(request)


async def register_endpoint(request: Request) -> JSONResponse:
    if not _rate_limit(request, "register"):
        return JSONResponse({"error": "slow_down"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "redirect_uris required"},
            status_code=400,
        )
    cleaned: list[str] = []
    for uri in redirect_uris:
        uri = str(uri).strip()
        err = _validate_redirect_uri(uri)
        if err:
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": f"{uri}: {err}"},
                status_code=400,
            )
        cleaned.append(uri)
    client_id = secrets.token_urlsafe(16)
    global oauth_clients
    oauth_clients = _load_oauth_clients()
    oauth_clients[client_id] = {
        "redirect_uris": cleaned,
        "created_at": time.time(),
    }
    _save_oauth_clients(oauth_clients)
    return JSONResponse({
        "client_id": client_id,
        "client_secret": "",
        "redirect_uris": cleaned,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


routes = [
    Route("/.well-known/oauth-authorization-server", endpoint=oauth_authorization_server_endpoint, methods=["GET"]),
    Route("/.well-known/oauth-protected-resource",   endpoint=oauth_protected_resource_endpoint,   methods=["GET"]),
    Route("/authorize",     endpoint=authorize_router,          methods=["GET", "POST"]),
    Route("/register",      endpoint=register_endpoint,         methods=["POST"]),
    Route("/token",         endpoint=token_endpoint,            methods=["POST"]),
    Route("/token/refresh", endpoint=token_refresh_endpoint,    methods=["POST"]),  # alias
    Route("/health",        endpoint=health_endpoint,           methods=["GET"]),
    Route("/sse",           endpoint=sse_endpoint,              methods=["GET"]),
    Route("/messages",      endpoint=messages_endpoint,         methods=["POST"]),
    Route("/mcp",           endpoint=mcp_streamable_endpoint,   methods=["GET", "POST"]),  # ChatGPT/Perplexity
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version", "Mcp-Session-Id", "Mcp-Method", "Mcp-Name", "Last-Event-ID"],
    )
]

app = Starlette(routes=routes, middleware=middleware)


def main(host: str = "0.0.0.0", port: int = 8000) -> None:
    bootstrap()
    uvicorn.run(app, host=host, port=port, log_level="info", reload=False)


if __name__ == "__main__":
    main()
