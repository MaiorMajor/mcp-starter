#!/usr/bin/env python3
"""
vault-dispatch — Resolve queries para destinos no vault.
Determinístico, pure Python, 0 tokens LLM.

Uso:
    python main.py "DRAPN automação bug"
    python main.py "dopamina hábitos" --output markdown
    python main.py "letra nova rap" --top 3
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from mcp_starter.vault_layout import CAPTURE_FALLBACK, META  # noqa: E402

def _vault_root() -> Path:
    env = os.environ.get("VAULT_PATH") or os.environ.get("VAULT_ROOT")
    if env:
        return Path(env)
    raise SystemExit("VAULT_PATH must be set in the environment.")

# Pastas excluídas do dispatch (não são destinos válidos para conteúdo)
EXCLUDE_DESTINATIONS = {".git", ".obsidian", META, "_archive"}

# Pastas que nunca devem ser sugeridas
FORBIDDEN = {"_PRIVADO"}

# Hints de keywords por skill — carregados de skill_hints.json em runtime.
# NÃO editar aqui. Editar: skill_hints.json na raiz do repo mcp-starter.
_SKILL_HINTS_PATH = REPO_ROOT / "skill_hints.json"
_SKILL_HINTS_CACHE: dict | None = None


def _load_skill_hints() -> dict:
    global _SKILL_HINTS_CACHE
    if _SKILL_HINTS_CACHE is not None:
        return _SKILL_HINTS_CACHE
    if _SKILL_HINTS_PATH.exists():
        try:
            raw = json.loads(_SKILL_HINTS_PATH.read_text(encoding="utf-8"))
            _SKILL_HINTS_CACHE = {
                k: [h.lower() for h in v]
                for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, list)
            }
            return _SKILL_HINTS_CACHE
        except Exception:  # noqa: BLE001
            pass
    return {}


# ── Frontmatter parser ───────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> dict:
    """Extrai frontmatter YAML simples (sem dependências externas)."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fm = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            # Parse inline list: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                fm[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            else:
                fm[key] = val.strip("'\"")
    return fm


# ── Index builder ─────────────────────────────────────────────────────────────

def build_keyword_index(vault: Path) -> list[dict]:
    """Constrói índice keyword → destino a partir de CONTEXT.md, _index.md e nomes de pastas."""
    entries = []

    # 1. Scan all CONTEXT.md files
    for ctx_file in vault.rglob("CONTEXT.md"):
        rel = ctx_file.relative_to(vault)
        # Skip archive, meta, privado
        if any(part in EXCLUDE_DESTINATIONS or part in FORBIDDEN for part in rel.parts):
            continue

        text = ctx_file.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)

        # Destination is the parent folder of CONTEXT.md
        dest = str(rel.parent).replace("\\", "/")
        if dest == ".":
            continue  # Root CONTEXT.md is routing layer, not a destination

        keywords = []

        # From frontmatter keywords field
        if "keywords" in fm:
            if isinstance(fm["keywords"], list):
                keywords.extend(fm["keywords"])
            elif isinstance(fm["keywords"], str):
                keywords.extend([k.strip() for k in fm["keywords"].split(",") if k.strip()])

        # From folder name parts
        for part in rel.parent.parts:
            if part not in EXCLUDE_DESTINATIONS and not part.startswith("_"):
                keywords.append(part.replace("-", " ").replace("_", " "))
                keywords.append(part)  # Also add raw name

        # Parse routing table in CONTEXT.md body (| Task | Destino | format)
        keywords.extend(_extract_routing_keywords(text, dest))

        # Context files for this destination (only CONTEXT.md — _index.md is structural)
        context_files = [str(rel).replace("\\", "/")]
        runtime = ctx_file.parent / "_runtime" / "state.md"
        has_runtime = runtime.exists()

        entries.append({
            "destination": dest,
            "keywords": list(set(k.lower() for k in keywords if k)),
            "context_files": context_files,
            "has_runtime": has_runtime,
        })

    # 2. Root CONTEXT.md routing table (maps keywords → specific destinations)
    root_ctx = vault / "CONTEXT.md"
    if root_ctx.exists():
        text = root_ctx.read_text(encoding="utf-8", errors="replace")
        for kw, dest in _parse_root_routing(text):
            # Find or create entry for this destination
            existing = next((e for e in entries if e["destination"] == dest), None)
            if existing:
                if kw.lower() not in existing["keywords"]:
                    existing["keywords"].append(kw.lower())
            else:
                entries.append({
                    "destination": dest,
                    "keywords": [kw.lower()],
                    "context_files": [],
                    "has_runtime": (vault / dest / "_runtime" / "state.md").exists(),
                })

    return entries


def _extract_routing_keywords(text: str, dest: str) -> list[str]:
    """Extrai keywords das tabelas de routing markdown."""
    keywords = []
    # Match table rows: | something | `dest/...` |
    for m in re.finditer(r"\|\s*([^|]+?)\s*\|\s*`?([^|`]+)`?\s*\|", text):
        task_col = m.group(1).strip()
        dest_col = m.group(2).strip()
        # Skip header rows
        if task_col.startswith("---") or task_col.lower() in ("task", "conteúdo", "conteudo", "content"):
            continue
        if "preencher" in task_col.lower():
            continue
        # Add task column words as keywords
        for word in re.split(r"[/,\s]+", task_col):
            word = word.strip("().*")
            if len(word) > 2:
                keywords.append(word)
    return keywords


def _parse_root_routing(text: str) -> list[tuple[str, str]]:
    """Parseia a routing table do CONTEXT.md raiz."""
    pairs = []
    for m in re.finditer(r"\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|", text):
        task = m.group(1).strip()
        dest = m.group(2).strip().rstrip("/")
        if task.startswith("---") or task.lower() in ("task", "conteúdo"):
            continue
        for word in re.split(r"[/,\s]+", task):
            word = word.strip("().*")
            if len(word) > 2:
                pairs.append((word, dest))
    return pairs


# ── Graph hints / context digest (ICM token optimization) ───────────────────

CONTEXT_READ_CONFIDENCE = 0.7


def _graph_hints(destination: str) -> list[str]:
    """Args para vault-graph query neighbors (sem ler JSON)."""
    return ["query", "neighbors", destination, "--depth", "1"]


def _context_digest(vault: Path, context_rel: str, max_bullets: int = 3) -> list[str]:
    """Extrai 1–3 bullets do corpo do CONTEXT (após frontmatter), sem tabelas."""
    p = vault / context_rel
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        parts = text.split("---", 2)
        body = parts[2] if len(parts) >= 3 else text
    else:
        body = text
    bullets = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("|") or s.startswith("#"):
            continue
        if s.startswith(">"):
            s = s.lstrip("> ").strip()
        if s.startswith("- "):
            bullets.append(s[2:].strip()[:200])
        elif len(s) > 20 and not s.startswith("```"):
            bullets.append(s[:200])
        if len(bullets) >= max_bullets:
            break
    return bullets


def _enrich_match(vault: Path, match: dict) -> dict:
    dest = match["destination"]
    match["graph_hints"] = _graph_hints(dest)
    ctx_files = match.get("context_files") or []
    digest = []
    for cf in ctx_files[:1]:
        digest.extend(_context_digest(vault, cf))
    match["context_digest"] = digest[:3]
    conf = match.get("confidence", 0)
    if conf >= CONTEXT_READ_CONFIDENCE and not match.get("has_runtime"):
        match["context_files_optional"] = True
        match["context_read_hint"] = (
            "Lê CONTEXT.md só se precisares de detalhe local ou confidence baixa; "
            "usa context_digest + vault-graph primeiro."
        )
    else:
        match["context_files_optional"] = False
        match["context_read_hint"] = "Lê context_files e _runtime/state.md se has_runtime."
    return match


# ── Matching ──────────────────────────────────────────────────────────────────

def dispatch(query: str, index: list[dict], top: int = 3, vault: Path | None = None) -> dict:
    """Resolve query → destinos ranked por score."""
    query_lower = query.lower()
    query_words = set(re.split(r"[\s,/]+", query_lower))
    query_words = {w for w in query_words if len(w) > 1}

    scored = []
    for entry in index:
        score = 0
        matched = []
        for kw in entry["keywords"]:
            # Exact word match (higher weight)
            if kw in query_words:
                score += 3
                matched.append(kw)
            # Substring match in full query
            elif kw in query_lower and len(kw) > 3:
                score += 2
                matched.append(kw)
            # Partial word overlap
            else:
                for qw in query_words:
                    if len(qw) > 3 and len(kw) > 3:
                        if qw in kw or kw in qw:
                            score += 1
                            matched.append(kw)
                            break

        if score > 0:
            # Bonus: more specific paths score higher
            depth_bonus = entry["destination"].count("/") * 0.5
            total = score + depth_bonus
            scored.append({
                "destination": entry["destination"],
                "confidence": min(total / (len(query_words) * 3 + 1), 0.99),
                "keywords_matched": sorted(set(matched)),
                "context_files": entry["context_files"],
                "applicable_skills": _suggest_skills(query_words, entry["destination"]),
                "has_runtime": entry["has_runtime"],
                "raw_score": total,
            })

    scored.sort(key=lambda x: x["raw_score"], reverse=True)

    if not scored:
        fb = {
            "destination": CAPTURE_FALLBACK,
            "confidence": 0.0,
            "keywords_matched": [],
            "context_files": ["CONTEXT.md"],
            "applicable_skills": [],
            "has_runtime": False,
        }
        if vault is not None:
            _enrich_match(vault, fb)
        return {"query": query, "matches": [fb], "fallback": True}

    # Clean up raw_score from output
    results = scored[:top]
    for r in results:
        del r["raw_score"]
        if vault is not None:
            _enrich_match(vault, r)

    return {
        "query": query,
        "matches": results,
        "fallback": False,
    }


def _suggest_skills(query_words: set, destination: str) -> list[str]:
    """Sugere skills relevantes com base na query e destino."""
    suggested = []
    all_words = query_words | set(destination.replace("/", " ").replace("-", " ").split())
    for skill, hints in _load_skill_hints().items():
        for hint in hints:
            if hint in all_words or any(hint in w or w in hint for w in all_words if len(w) > 3):
                if skill not in suggested:
                    suggested.append(skill)
                break
    return suggested


# ── Output formatters ─────────────────────────────────────────────────────────

def format_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def format_markdown(result: dict) -> str:
    lines = [f"# Dispatch: {result['query']}", ""]

    if result["fallback"]:
        lines.append("⚠️ No strong match found. Default destination: `inbox/` or your vault capture folder.")
        lines.append("")
        lines.append("Ask the user to clarify the topic or pick from the suggestions above.")
        return "\n".join(lines)

    for i, m in enumerate(result["matches"], 1):
        lines.append(f"## #{i} — `{m['destination']}`")
        lines.append(f"- **Confiança:** {m['confidence']:.0%}")
        lines.append(f"- **Keywords:** {', '.join(m['keywords_matched'])}")
        if m.get("graph_hints"):
            lines.append(f"- **Graph:** `vault-graph {' '.join(m['graph_hints'])}`")
        if m.get("context_digest"):
            for b in m["context_digest"]:
                lines.append(f"- {b}")
        if m["context_files"]:
            opt = " (opcional)" if m.get("context_files_optional") else ""
            lines.append(f"- **Contexto{opt}:** {', '.join(f'`{f}`' for f in m['context_files'])}")
        if m.get("context_read_hint"):
            lines.append(f"- {m['context_read_hint']}")
        if m["applicable_skills"]:
            lines.append(f"- **Skills:** {', '.join(m['applicable_skills'])}")
        if m["has_runtime"]:
            lines.append("- **⚡ Tem `_runtime/state.md`** — lê antes de agir")
        lines.append("")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Windows/cp1252: garante que emojis e acentos no output não rebentam.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="vault-dispatch: resolve query → destino no vault")
    parser.add_argument("query", help="Texto livre, keywords, frase descritiva")
    parser.add_argument("--vault", type=Path, default=None, help="Caminho raiz do vault")
    parser.add_argument("--output", choices=["json", "markdown"], default="json")
    parser.add_argument("--top", type=int, default=3, help="Nº máximo de destinos")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Mostra keywords matched e graph_hints (debug, sem LLM)",
    )
    args = parser.parse_args()

    vault = args.vault
    if vault is None:
        env = os.getenv("VAULT_PATH")
        if env:
            vault = Path(env)
        else:
            vault = _vault_root()
    vault = vault.resolve()

    if not vault.exists() or not vault.is_dir():
        sys.exit(f"Error: vault not found at {vault}")

    index = build_keyword_index(vault)
    result = dispatch(args.query, index, top=args.top, vault=vault)

    if args.explain and args.output == "json":
        result["index_size"] = len(index)
        result["explain"] = True

    if args.output == "json":
        print(format_json(result))
    else:
        print(format_markdown(result))


if __name__ == "__main__":
    main()
