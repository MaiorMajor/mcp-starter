"""
Shared vault file discovery — used by MCP find_files and vault-find skill.
Returns metadata only (path, ext, size, dates); does not read file contents.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

TYPE_EXTENSIONS: dict[str, set[str]] = {
    "ebooks": {".epub", ".mobi", ".azw", ".azw3", ".fb2"},
    "docs": {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md"},
    "images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".tiff", ".bmp", ".heic"},
    "audio": {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"},
    "code": {".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml", ".sh", ".toml"},
    "data": {".csv", ".xlsx", ".xls", ".sqlite", ".db", ".jsonl", ".parquet"},
    "archives": {".zip", ".tar", ".gz", ".rar", ".7z"},
}

FORBIDDEN_DIRS = {"_PRIVADO", ".git", "node_modules", "__pycache__", ".venv", ".cache", ".obsidian"}
FORBIDDEN_PATHS = ("00_inbox/simplenote",)

FIND_FILES_DEFAULT_LIMIT = 100
FIND_FILES_MCP_MAX = 100


def is_hot_path(rel: str) -> bool:
    return any(rel.startswith(p) for p in FORBIDDEN_PATHS)


def parse_dt(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"invalid datetime: {value!r} (expected YYYY-MM-DD or YYYY-MM-DDTHH:MM)")


@dataclass
class FindFilesParams:
    name: Optional[str] = None
    ext: Optional[str] = None
    file_type: Optional[str] = None  # MCP/CLI arg is still "type"
    path_contains: Optional[str] = None
    modified_after: Optional[str] = None
    modified_before: Optional[str] = None
    created_after: Optional[str] = None
    created_before: Optional[str] = None
    today: bool = False
    yesterday: bool = False
    min_size: Optional[int] = None
    max_size: Optional[int] = None
    sort_by: Optional[str] = None
    limit: int = FIND_FILES_DEFAULT_LIMIT
    exclude_md: bool = False


def search_files(vault: Path, params: FindFilesParams) -> dict:
    """Walk vault and return matching file metadata (no content)."""
    target_exts: Optional[set[str]] = None
    if params.ext:
        target_exts = {
            e.strip() if e.strip().startswith(".") else f".{e.strip()}"
            for e in params.ext.split(",")
            if e.strip()
        }
    if params.file_type:
        type_exts = TYPE_EXTENSIONS.get(params.file_type)
        if type_exts is None:
            return {
                "error": f"unknown type {params.file_type!r}; valid: {', '.join(TYPE_EXTENSIONS)}",
                "files": [],
                "count": 0,
            }
        target_exts = (target_exts or set()) | type_exts

    today_start = datetime.combine(date.today(), datetime.min.time())
    yesterday_start = today_start - timedelta(days=1)

    modified_after = modified_before = created_after = created_before = None
    if params.today:
        modified_after = today_start
        created_after = today_start
    elif params.yesterday:
        modified_after = yesterday_start
        modified_before = today_start
        created_after = yesterday_start
        created_before = today_start
    else:
        try:
            if params.modified_after:
                modified_after = parse_dt(params.modified_after)
            if params.modified_before:
                modified_before = parse_dt(params.modified_before)
            if params.created_after:
                created_after = parse_dt(params.created_after)
            if params.created_before:
                created_before = parse_dt(params.created_before)
        except ValueError as exc:
            return {"error": str(exc), "files": [], "count": 0}

    name_pat = params.name
    if name_pat and "*" not in name_pat and "?" not in name_pat:
        name_pat = f"*{name_pat}*"

    results: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in FORBIDDEN_DIRS]
        for fname in filenames:
            if fname.startswith("."):
                continue
            full = Path(dirpath) / fname
            try:
                rel = str(full.relative_to(vault)).replace("\\", "/")
            except ValueError:
                continue
            if is_hot_path(rel):
                continue

            ext = full.suffix.lower()
            if params.exclude_md and ext == ".md":
                continue
            if target_exts is not None and ext not in target_exts:
                continue
            if name_pat and not fnmatch(fname.lower(), name_pat.lower()):
                continue
            if params.path_contains and params.path_contains.lower() not in rel.lower():
                continue

            try:
                st = full.stat()
            except OSError:
                continue

            mtime = datetime.fromtimestamp(st.st_mtime)
            ctime_ts = getattr(st, "st_birthtime", st.st_ctime)
            ctime = datetime.fromtimestamp(ctime_ts)

            if modified_after and mtime < modified_after:
                continue
            if modified_before and mtime >= modified_before:
                continue
            if created_after and ctime < created_after:
                continue
            if created_before and ctime >= created_before:
                continue
            if params.min_size is not None and st.st_size < params.min_size:
                continue
            if params.max_size is not None and st.st_size > params.max_size:
                continue

            results.append({
                "path": rel,
                "name": fname,
                "ext": ext,
                "size_bytes": st.st_size,
                "modified": mtime.strftime("%Y-%m-%dT%H:%M:%S"),
                "ctime": ctime.strftime("%Y-%m-%dT%H:%M:%S"),
            })

    use_created = params.today or params.yesterday or bool(created_after)
    sort_key = params.sort_by or ("created" if use_created else "modified")

    if sort_key == "name":
        results.sort(key=lambda r: r["name"].lower())
    elif sort_key == "size":
        results.sort(key=lambda r: r["size_bytes"], reverse=True)
    elif sort_key == "created":
        results.sort(key=lambda r: r["ctime"], reverse=True)
    else:
        results.sort(key=lambda r: r["modified"], reverse=True)

    total = len(results)
    truncated = bool(params.limit and total > params.limit)
    if truncated:
        results = results[: params.limit]

    out: dict = {"files": results, "count": len(results), "total_matched": total}
    if truncated:
        out["truncated"] = True
        out["hint"] = f"Results capped at {params.limit}. Narrow filters (ext, path_contains, name)."
    return out
