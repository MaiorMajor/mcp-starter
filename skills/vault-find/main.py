#!/usr/bin/env python3
"""
vault-find v2 — pesquisa avançada de ficheiros no vault por nome, extensão, tipo, data, tamanho.
v2: adiciona --created-after/before, --today, --yesterday, --sort-by, ctime no output, datetime completo.
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_DIR = REPO_ROOT
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

from vault_file_search import FindFilesParams, TYPE_EXTENSIONS, search_files  # noqa: E402


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _peek_title(full: Path, ext: str) -> str:
    """Devolve title do frontmatter ou 1º H1 (apenas .md). 1 linha, truncado a 90 chars."""
    if ext != ".md":
        return ""
    try:
        with full.open(encoding="utf-8", errors="replace") as f:
            head = f.read(1500)
    except OSError:
        return ""
    title = ""
    if head.startswith("---"):
        end = head.find("\n---", 3)
        if end != -1:
            for line in head[3:end].splitlines():
                line = line.strip()
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"\'')
                    break
    if not title:
        for line in head.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                break
    title = " ".join(title.split())
    return title[:90]


def print_table(results: list[dict]) -> None:
    if not results:
        print("Nenhum ficheiro encontrado.")
        return
        print(f"{'EXT':<8} {'SIZE':>8}  {'MODIFIED':>19}  {'CTIME':>19}  PATH")
        print("─" * 110)
        for r in results:
            print(f"{r['ext']:<8} {human_size(r['size_bytes']):>8}  {r['modified']:>19}  {r['ctime']:>19}  {r['path']}")
    print(f"\n{len(results)} resultado(s)")


def print_brief(vault: Path, results: list[dict]) -> None:
    if not results:
        print("Nenhum ficheiro encontrado.")
        return
    for r in results:
        title = _peek_title(vault / r["path"], r["ext"])
        if title:
            print(f"{r['path']}\t{title}")
        else:
            print(r["path"])


def main() -> None:
    # Windows/cp1252: garante que emojis e acentos no output não rebentam.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="vault-find v2 — pesquisa avançada de ficheiros no vault")
    parser.add_argument("query", nargs="?", help="Substring no nome (atalho para --name)")
    parser.add_argument("--name",            help="Padrão glob no nome (ex: *kleon*, *.epub)")
    parser.add_argument("--ext",             help="Extensões separadas por vírgula (ex: .epub,.pdf,.md)")
    parser.add_argument("--type",            choices=list(TYPE_EXTENSIONS), metavar="TYPE")
    parser.add_argument("--path-contains",   dest="path_contains")
    parser.add_argument("--modified-after",  dest="modified_after",  metavar="YYYY-MM-DD[THH:MM]")
    parser.add_argument("--modified-before", dest="modified_before", metavar="YYYY-MM-DD[THH:MM]")
    parser.add_argument("--created-after",   dest="created_after",   metavar="YYYY-MM-DD[THH:MM]")
    parser.add_argument("--created-before",  dest="created_before",  metavar="YYYY-MM-DD[THH:MM]")
    parser.add_argument("--today",     action="store_true")
    parser.add_argument("--yesterday", action="store_true")
    parser.add_argument("--sort-by",   dest="sort_by", choices=["modified", "created", "name", "size"])
    parser.add_argument("--min-size",  dest="min_size", type=int, metavar="BYTES")
    parser.add_argument("--max-size",  dest="max_size", type=int, metavar="BYTES")
    parser.add_argument("--limit",     type=int, default=100)
    parser.add_argument("--vault",     default=None)
    parser.add_argument("--output",    choices=["table", "json", "brief"], default="table")
    parser.add_argument("--brief",     action="store_true",
                        help="Atalho para --output brief (path + 1 linha de title por ficheiro).")

    args = parser.parse_args()
    if args.query and not args.name:
        args.name = args.query
    limit = None if args.limit == 0 else args.limit

    vault = Path(args.vault).resolve() if args.vault else Path(os.environ.get("VAULT_PATH", os.environ.get("VAULT_ROOT", "")))
    if not vault or not str(vault):
        print("ERRO: defina VAULT_PATH ou use --vault", file=sys.stderr)
        sys.exit(1)
    if not vault.exists():
        print(f"ERRO: vault não encontrado em '{vault}'", file=sys.stderr)
        sys.exit(1)

    params = FindFilesParams(
        name=args.name,
        ext=args.ext,
        file_type=args.type,
        path_contains=args.path_contains,
        modified_after=args.modified_after,
        modified_before=args.modified_before,
        created_after=args.created_after,
        created_before=args.created_before,
        today=args.today,
        yesterday=args.yesterday,
        min_size=args.min_size,
        max_size=args.max_size,
        sort_by=args.sort_by,
        limit=limit or 100,
    )
    out = search_files(vault, params)
    if out.get("error"):
        print(f"ERRO: {out['error']}", file=sys.stderr)
        sys.exit(1)
    results = out.get("files") or []

    if args.brief and args.output == "table":
        args.output = "brief"

    if args.output == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.output == "brief":
        print_brief(vault, results)
    else:
        print_table(results)


if __name__ == "__main__":
    main()
