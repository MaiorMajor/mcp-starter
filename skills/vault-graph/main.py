#!/usr/bin/env python3
"""vault-graph — extrai e consulta o grafo de wikilinks + tags do vault.

Dois modos:
  1. Geração:  `python main.py` (default) → escreve 99_meta/vault-graph.json
  2. Query:    `python main.py query <subcmd>` → resposta pequena e específica

Queries (todos read-only sobre o JSON, NUNCA leiam o ficheiro inteiro via MCP):
  backlinks <nota>            quem aponta para <nota>
  forward   <nota>            para onde <nota> aponta
  neighbors <nota> [--depth N --direction in|out|both]
  hubs      [--top N]         top in-degree
  authorities [--top N]       alias de hubs
  orphans   [--limit N]       nodes sem in nem out edges
  tag       <tag> [--limit N] notas com tag
  node      <nota>            ficha completa: tags, folder, in/out degree, samples
  path      <from> <to>       caminho mais curto (BFS, ignora direção)
  find      <pattern>         fuzzy match em basenames (ajuda a desambiguar)
  stats                       contagens globais e meta do graph

Resolução de <nota>: aceita basename ("PROFILE"), basename.md ou path completo
("99_meta/PROFILE.md"). Em colisões, devolve a 1ª resolução determinística e
sugere fuzzy alternatives.

Output: human-readable por defeito; `--json` para automação. Tudo limitado em
tamanho — nenhum subcomando devolve mais que algumas dezenas de linhas sem
--limit explícito.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
_vault_env = os.environ.get("VAULT_PATH") or os.environ.get("VAULT_ROOT")
if not _vault_env:
    raise SystemExit("VAULT_PATH must be set in the environment.")
VAULT_ROOT = Path(_vault_env)
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from mcp_starter.vault_layout import GRAPH_JSON, GRAPH_STALE, INBOX, META  # noqa: E402

GRAPH_OUT = VAULT_ROOT / GRAPH_JSON
STALE_FLAG = VAULT_ROOT / GRAPH_STALE

EXCLUDE_DIR_PARTS = {
    f"{INBOX}/.capture",
    "_PRIVADO",
    "__pycache__",
    ".obsidian",
    ".trash",
    ".git",
    ".venv",
    "node_modules",
    ".cline",
    ".agents",
    f"{META}/_archive",
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

sys.path.insert(0, str(REPO_ROOT / "skills"))
try:
    from _shared.guards import check_circuit_breaker
except Exception:
    def check_circuit_breaker():
        return None


# ────────────────────────── GERAÇÃO ──────────────────────────

def is_excluded(rel_path: str) -> bool:
    return any(part in rel_path for part in EXCLUDE_DIR_PARTS)


def parse_frontmatter_tags(text: str) -> list[str]:
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end < 0:
        return []
    block = text[3:end]
    tags: list[str] = []
    in_tags_block = False
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            in_tags_block = False
            continue
        m = re.match(r"^tags\s*:\s*(.*)$", stripped)
        if m:
            value = m.group(1).strip()
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1]
                tags.extend(t.strip().strip("'\"") for t in inner.split(",") if t.strip())
                in_tags_block = False
            elif value:
                tags.append(value.strip("'\""))
                in_tags_block = False
            else:
                in_tags_block = True
            continue
        if in_tags_block:
            m2 = re.match(r"^-\s*(.+)$", line.strip())
            if m2:
                tags.append(m2.group(1).strip().strip("'\""))
            else:
                in_tags_block = False
    return [t for t in tags if t]


def extract_wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(text)]


def normalize_link(target: str, all_basenames: dict[str, str]) -> str | None:
    base = target.replace("\\", "/").split("/")[-1]
    if base.endswith(".md"):
        base = base[:-3]
    return all_basenames.get(base.lower())


def build_graph(vault_root: Path) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    basenames: dict[str, str] = {}
    raw_links: dict[str, list[str]] = {}

    for path in vault_root.rglob("*.md"):
        rel = path.relative_to(vault_root).as_posix()
        if is_excluded(rel):
            continue
        base = path.stem.lower()
        basenames.setdefault(base, rel)

    for path in vault_root.rglob("*.md"):
        rel = path.relative_to(vault_root).as_posix()
        if is_excluded(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        tags = parse_frontmatter_tags(text)
        links = extract_wikilinks(text)
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        nodes.append({"id": rel, "tags": tags, "folder": folder})
        raw_links[rel] = links

    for source, targets in raw_links.items():
        for target in targets:
            resolved = normalize_link(target, basenames)
            if resolved and resolved != source:
                edges.append({"source": source, "target": resolved, "type": "wikilink"})

    return {
        "generated_at": datetime.now().isoformat(),
        "vault_root": str(vault_root),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def top_hubs(graph: dict, k: int = 10) -> list[tuple[str, int]]:
    in_degree: Counter = Counter()
    for e in graph["edges"]:
        in_degree[e["target"]] += 1
    return in_degree.most_common(k)


# ────────────────────────── QUERY LAYER ──────────────────────────

class GraphIndex:
    """Índices pré-computados sobre o JSON, construídos uma vez por invocação."""

    def __init__(self, graph: dict):
        self.graph = graph
        self.nodes_by_id: dict[str, dict] = {n["id"]: n for n in graph["nodes"]}
        self.basenames: dict[str, list[str]] = defaultdict(list)
        for nid in self.nodes_by_id:
            base = nid.rsplit("/", 1)[-1].removesuffix(".md").lower()
            self.basenames[base].append(nid)
        self.out_edges: dict[str, list[str]] = defaultdict(list)
        self.in_edges: dict[str, list[str]] = defaultdict(list)
        for e in graph["edges"]:
            self.out_edges[e["source"]].append(e["target"])
            self.in_edges[e["target"]].append(e["source"])

    def resolve(self, query: str) -> tuple[str | None, list[str]]:
        """Devolve (id_resolvido, candidatos_se_ambiguo).

        Estratégia:
          1. Match exacto de path → único hit.
          2. Match exacto de basename (case-insensitive) → único hit ou lista de candidatos.
          3. Fuzzy substring sobre paths/basenames → sugestões.
        """
        q = query.strip()
        if q in self.nodes_by_id:
            return q, []
        base = q.rsplit("/", 1)[-1].removesuffix(".md").lower()
        candidates = self.basenames.get(base, [])
        if len(candidates) == 1:
            return candidates[0], []
        if len(candidates) > 1:
            return candidates[0], candidates
        ql = q.lower()
        fuzzy = [nid for nid in self.nodes_by_id if ql in nid.lower()]
        return (None, fuzzy[:10])


def load_graph() -> dict:
    if not GRAPH_OUT.exists():
        print(f"ERRO: graph não existe em {GRAPH_OUT}. Corre `python main.py` primeiro.", file=sys.stderr)
        sys.exit(2)
    return json.loads(GRAPH_OUT.read_text(encoding="utf-8"))


def emit(payload, json_out: bool, human_fn=None):
    if json_out:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif human_fn:
        human_fn(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False))


def _resolve_or_die(idx: GraphIndex, query: str, json_out: bool) -> str:
    rid, candidates = idx.resolve(query)
    if rid is None:
        msg = {"error": "not_found", "query": query, "suggestions": candidates}
        if json_out:
            print(json.dumps(msg, ensure_ascii=False, indent=2))
        else:
            print(f"Não encontrei '{query}'.")
            if candidates:
                print("Sugestões:")
                for c in candidates:
                    print(f"  {c}")
        sys.exit(3)
    if candidates and len(candidates) > 1:
        # Ambiguidade — devolveu o 1º, mas avisa em stderr
        print(f"AVISO: '{query}' é ambíguo, escolhi {rid}. Outros: {candidates[1:5]}", file=sys.stderr)
    return rid


def q_backlinks(idx: GraphIndex, args):
    rid = _resolve_or_die(idx, args.note, args.json_out)
    sources = sorted(set(idx.in_edges.get(rid, [])))
    limited = sources[: args.limit] if args.limit else sources
    payload = {"node": rid, "in_degree": len(sources), "backlinks": limited, "truncated": len(sources) > len(limited)}

    def human(p):
        print(f"← {p['node']}  ({p['in_degree']} backlinks)")
        for s in p["backlinks"]:
            print(f"  {s}")
        if p["truncated"]:
            print(f"  ... +{p['in_degree'] - len(p['backlinks'])} (usa --limit)")
    emit(payload, args.json_out, human)


def q_forward(idx: GraphIndex, args):
    rid = _resolve_or_die(idx, args.note, args.json_out)
    targets = sorted(set(idx.out_edges.get(rid, [])))
    limited = targets[: args.limit] if args.limit else targets
    payload = {"node": rid, "out_degree": len(targets), "forward": limited, "truncated": len(targets) > len(limited)}

    def human(p):
        print(f"→ {p['node']}  ({p['out_degree']} outgoing)")
        for t in p["forward"]:
            print(f"  {t}")
        if p["truncated"]:
            print(f"  ... +{p['out_degree'] - len(p['forward'])} (usa --limit)")
    emit(payload, args.json_out, human)


def q_neighbors(idx: GraphIndex, args):
    rid = _resolve_or_die(idx, args.note, args.json_out)
    depth = max(1, args.depth)
    direction = args.direction
    visited: dict[str, int] = {rid: 0}
    frontier = deque([rid])
    while frontier:
        cur = frontier.popleft()
        d = visited[cur]
        if d >= depth:
            continue
        nexts: list[str] = []
        if direction in ("out", "both"):
            nexts.extend(idx.out_edges.get(cur, []))
        if direction in ("in", "both"):
            nexts.extend(idx.in_edges.get(cur, []))
        for n in nexts:
            if n not in visited:
                visited[n] = d + 1
                frontier.append(n)
    by_depth: dict[int, list[str]] = defaultdict(list)
    for n, d in visited.items():
        if n != rid:
            by_depth[d].append(n)
    payload = {
        "node": rid,
        "depth": depth,
        "direction": direction,
        "total": len(visited) - 1,
        "by_depth": {str(d): sorted(v)[: args.limit] for d, v in sorted(by_depth.items())},
    }

    def human(p):
        print(f"⊙ {p['node']}  vizinhos até depth={p['depth']} ({p['direction']}): {p['total']}")
        for d_str, nodes in p["by_depth"].items():
            print(f"  depth {d_str}: {len(nodes)}")
            for n in nodes[:20]:
                print(f"    {n}")
            if len(nodes) > 20:
                print(f"    ... +{len(nodes) - 20}")
    emit(payload, args.json_out, human)


def q_hubs(idx: GraphIndex, args):
    in_deg = Counter({nid: len(srcs) for nid, srcs in idx.in_edges.items()})
    top = in_deg.most_common(args.top)
    payload = {"top": args.top, "hubs": [{"id": nid, "in_degree": d} for nid, d in top]}

    def human(p):
        print(f"Top {p['top']} hubs por in-degree:")
        for h in p["hubs"]:
            print(f"  {h['in_degree']:5d}  {h['id']}")
    emit(payload, args.json_out, human)


def q_orphans(idx: GraphIndex, args):
    orphans = [nid for nid in idx.nodes_by_id if not idx.in_edges.get(nid) and not idx.out_edges.get(nid)]
    orphans.sort()
    limited = orphans[: args.limit]
    payload = {"total": len(orphans), "orphans": limited, "truncated": len(orphans) > len(limited)}

    def human(p):
        print(f"Orphans: {p['total']}  (mostra {len(p['orphans'])})")
        for n in p["orphans"]:
            print(f"  {n}")
        if p["truncated"]:
            print(f"  ... +{p['total'] - len(p['orphans'])}")
    emit(payload, args.json_out, human)


def q_tag(idx: GraphIndex, args):
    tag = args.tag.lstrip("#").lower()
    hits = [nid for nid, n in idx.nodes_by_id.items() if any(t.lower() == tag for t in n.get("tags", []))]
    hits.sort()
    limited = hits[: args.limit]
    payload = {"tag": tag, "total": len(hits), "notes": limited, "truncated": len(hits) > len(limited)}

    def human(p):
        print(f"#{p['tag']}: {p['total']} notas")
        for n in p["notes"]:
            print(f"  {n}")
        if p["truncated"]:
            print(f"  ... +{p['total'] - len(p['notes'])}")
    emit(payload, args.json_out, human)


def q_node(idx: GraphIndex, args):
    rid = _resolve_or_die(idx, args.note, args.json_out)
    n = idx.nodes_by_id[rid]
    payload = {
        "id": rid,
        "folder": n.get("folder", ""),
        "tags": n.get("tags", []),
        "in_degree": len(idx.in_edges.get(rid, [])),
        "out_degree": len(idx.out_edges.get(rid, [])),
        "backlinks_sample": sorted(set(idx.in_edges.get(rid, [])))[:5],
        "forward_sample": sorted(set(idx.out_edges.get(rid, [])))[:5],
    }

    def human(p):
        print(f"{p['id']}")
        print(f"  folder:     {p['folder']}")
        print(f"  tags:       {p['tags']}")
        print(f"  in-degree:  {p['in_degree']}")
        print(f"  out-degree: {p['out_degree']}")
        if p["backlinks_sample"]:
            print(f"  ← sample:")
            for s in p["backlinks_sample"]:
                print(f"      {s}")
        if p["forward_sample"]:
            print(f"  → sample:")
            for s in p["forward_sample"]:
                print(f"      {s}")
    emit(payload, args.json_out, human)


def q_path(idx: GraphIndex, args):
    a = _resolve_or_die(idx, args.from_note, args.json_out)
    b = _resolve_or_die(idx, args.to_note, args.json_out)
    if a == b:
        emit({"from": a, "to": b, "path": [a], "length": 0}, args.json_out,
             lambda p: print(f"{a} (mesmo node)"))
        return
    # BFS ignora direção (undirected)
    parents: dict[str, str | None] = {a: None}
    q: deque[str] = deque([a])
    found = False
    while q:
        cur = q.popleft()
        if cur == b:
            found = True
            break
        neighbours = set(idx.out_edges.get(cur, [])) | set(idx.in_edges.get(cur, []))
        for n in neighbours:
            if n not in parents:
                parents[n] = cur
                q.append(n)
    if not found:
        emit({"from": a, "to": b, "path": None, "length": None, "error": "no_path"}, args.json_out,
             lambda p: print(f"Sem caminho entre {a} e {b}."))
        return
    path = []
    cur: str | None = b
    while cur is not None:
        path.append(cur)
        cur = parents[cur]
    path.reverse()
    payload = {"from": a, "to": b, "path": path, "length": len(path) - 1}

    def human(p):
        print(f"Caminho ({p['length']} saltos):")
        for step in p["path"]:
            print(f"  {step}")
    emit(payload, args.json_out, human)


def q_find(idx: GraphIndex, args):
    ql = args.pattern.lower()
    hits = [nid for nid in idx.nodes_by_id if ql in nid.lower()]
    hits.sort()
    limited = hits[: args.limit]
    payload = {"pattern": args.pattern, "total": len(hits), "matches": limited, "truncated": len(hits) > len(limited)}

    def human(p):
        print(f"'{p['pattern']}': {p['total']} matches")
        for h in p["matches"]:
            print(f"  {h}")
        if p["truncated"]:
            print(f"  ... +{p['total'] - len(p['matches'])}")
    emit(payload, args.json_out, human)


def _schema_root_drift_count() -> int | None:
    """Optional anti-drift metric when meta/routing-audit tooling is present."""
    try:
        audit_dir = VAULT_ROOT / META / "routing-audit"
        if not audit_dir.exists():
            return None
        sys.path.insert(0, str(audit_dir))
        from schema_rules import count_root_files_non_leaf, load_schema  # noqa: WPS433

        return count_root_files_non_leaf(VAULT_ROOT, load_schema())
    except Exception:
        return None


def q_stats(idx: GraphIndex, args):
    g = idx.graph
    in_deg = [len(v) for v in idx.in_edges.values()]
    out_deg = [len(v) for v in idx.out_edges.values()]
    root_drift = _schema_root_drift_count()
    payload = {
        "generated_at": g.get("generated_at"),
        "vault_root": g.get("vault_root"),
        "node_count": g.get("node_count"),
        "edge_count": g.get("edge_count"),
        "nodes_with_in_edges": len(idx.in_edges),
        "nodes_with_out_edges": len(idx.out_edges),
        "orphans": sum(1 for nid in idx.nodes_by_id if not idx.in_edges.get(nid) and not idx.out_edges.get(nid)),
        "max_in_degree": max(in_deg) if in_deg else 0,
        "max_out_degree": max(out_deg) if out_deg else 0,
        "files_at_root_of_non_leaf_folder": root_drift,
    }

    def human(p):
        for k, v in p.items():
            print(f"  {k}: {v}")
    emit(payload, args.json_out, human)


# ────────────────────────── ARGPARSE ──────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Vault graph: gerar e consultar grafo de wikilinks + tags.")
    sub = p.add_subparsers(dest="cmd")

    # default (sem subcommand) = gerar
    p.add_argument("--dry-run", action="store_true", help="Só stats, não escreve graph.json")
    p.add_argument("--json", action="store_true", dest="json_out", help="Output JSON")

    q = sub.add_parser("query", help="Consultar o graph já gerado")
    qsub = q.add_subparsers(dest="qcmd", required=True)

    def add_json(parser):
        parser.add_argument("--json", action="store_true", dest="json_out", help="Output JSON")

    pb = qsub.add_parser("backlinks", help="Quem aponta para <nota>")
    pb.add_argument("note")
    pb.add_argument("--limit", type=int, default=50)
    add_json(pb)

    pf = qsub.add_parser("forward", help="Para onde <nota> aponta")
    pf.add_argument("note")
    pf.add_argument("--limit", type=int, default=50)
    add_json(pf)

    pn = qsub.add_parser("neighbors", help="Vizinhança N-hops")
    pn.add_argument("note")
    pn.add_argument("--depth", type=int, default=1)
    pn.add_argument("--direction", choices=["in", "out", "both"], default="both")
    pn.add_argument("--limit", type=int, default=50)
    add_json(pn)

    ph = qsub.add_parser("hubs", help="Top in-degree")
    ph.add_argument("--top", type=int, default=20)
    add_json(ph)

    pa = qsub.add_parser("authorities", help="Alias de hubs")
    pa.add_argument("--top", type=int, default=20)
    add_json(pa)

    po = qsub.add_parser("orphans", help="Nodes sem in nem out")
    po.add_argument("--limit", type=int, default=50)
    add_json(po)

    pt = qsub.add_parser("tag", help="Notas com tag")
    pt.add_argument("tag")
    pt.add_argument("--limit", type=int, default=50)
    add_json(pt)

    pnode = qsub.add_parser("node", help="Ficha completa do node")
    pnode.add_argument("note")
    add_json(pnode)

    ppath = qsub.add_parser("path", help="Caminho mais curto entre dois nodes (BFS undirected)")
    ppath.add_argument("from_note", metavar="from")
    ppath.add_argument("to_note", metavar="to")
    add_json(ppath)

    pfind = qsub.add_parser("find", help="Fuzzy match em paths")
    pfind.add_argument("pattern")
    pfind.add_argument("--limit", type=int, default=20)
    add_json(pfind)

    ps = qsub.add_parser("stats", help="Contagens globais")
    add_json(ps)

    return p


QUERY_DISPATCH = {
    "backlinks": q_backlinks,
    "forward": q_forward,
    "neighbors": q_neighbors,
    "hubs": q_hubs,
    "authorities": q_hubs,
    "orphans": q_orphans,
    "tag": q_tag,
    "node": q_node,
    "path": q_path,
    "find": q_find,
    "stats": q_stats,
}


def regenerate_if_stale() -> bool:
    """If the stale flag was touched (by an MCP write or similar), rebuild the graph
    and clear the flag before responding. Idempotent: no flag = no-op. Returns True
    if a regeneration happened. Failures are logged to stderr but do not raise —
    queries fall through to whatever (possibly stale) snapshot exists."""
    if not STALE_FLAG.exists():
        return False
    try:
        graph = build_graph(VAULT_ROOT)
        GRAPH_OUT.parent.mkdir(parents=True, exist_ok=True)
        GRAPH_OUT.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        STALE_FLAG.unlink(missing_ok=True)
        print(f"[vault-graph] auto-regenerated ({graph['node_count']} nodes, {graph['edge_count']} edges)", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[vault-graph] auto-regenerate FAILED, serving stale snapshot: {e}", file=sys.stderr)
        return False


def main() -> int:
    # Windows/cp1252: garante que emojis e acentos no output não rebentam.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    check_circuit_breaker()
    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "query":
        regenerate_if_stale()
        graph = load_graph()
        idx = GraphIndex(graph)
        fn = QUERY_DISPATCH.get(args.qcmd)
        if fn is None:
            parser.error(f"subcomando desconhecido: {args.qcmd}")
        fn(idx, args)
        return 0

    # Modo geração (default)
    if not VAULT_ROOT.exists():
        print(f"ERRO: VAULT_ROOT não existe: {VAULT_ROOT}", file=sys.stderr)
        return 1

    graph = build_graph(VAULT_ROOT)

    if not args.dry_run:
        GRAPH_OUT.parent.mkdir(parents=True, exist_ok=True)
        GRAPH_OUT.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        STALE_FLAG.unlink(missing_ok=True)

    root_drift = _schema_root_drift_count()
    if args.json_out:
        print(json.dumps({
            "node_count": graph["node_count"],
            "edge_count": graph["edge_count"],
            "files_at_root_of_non_leaf_folder": root_drift,
            "output": str(GRAPH_OUT) if not args.dry_run else None,
            "top_hubs": top_hubs(graph),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Nodes: {graph['node_count']}")
        print(f"Edges: {graph['edge_count']}")
        if root_drift is not None:
            print(f"files_at_root_of_non_leaf_folder: {root_drift}")
        if not args.dry_run:
            print(f"Output: {GRAPH_OUT.relative_to(VAULT_ROOT)}")
        print("\nTop 10 hubs (in-degree):")
        for name, deg in top_hubs(graph):
            print(f"  {deg:4d}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
