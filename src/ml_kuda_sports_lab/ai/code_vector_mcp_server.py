#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""MCP server for Chroma-backed code retrieval.

This server performs the retrieval step of RAG for Continue:
it embeds the user's question with Ollama/Nomic, searches the local Chroma
code index, and returns matching code chunks with file/line metadata.
"""

from __future__ import annotations

import os
import sys
import difflib
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ml_kuda_sports_lab.ai.chroma_code_index import (
    DEFAULT_COLLECTION,
    DEFAULT_DB_PATH,
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    format_matches_for_continue,
    search_code_index as run_code_index_search,
)


MCP = FastMCP("chroma-code-rag-retriever")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _safe_repo_path(file: str) -> Path:
    if not file.strip():
        raise ValueError("file is required")

    raw_path = Path(file).expanduser()
    path = raw_path if raw_path.is_absolute() else _repo_root() / raw_path
    resolved = path.resolve()
    root = _repo_root().resolve()

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to access path outside repo: {resolved}") from exc
    return resolved


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _settings() -> dict[str, Any]:
    return {
        "db_path": os.environ.get("CODE_INDEX_DB_PATH", DEFAULT_DB_PATH),
        "collection": os.environ.get("CODE_INDEX_COLLECTION", DEFAULT_COLLECTION),
        "ollama_base_url": os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        "model": os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        "default_file": os.environ.get("CODE_INDEX_DEFAULT_FILE", ""),
        "default_n_results": _env_int("CODE_INDEX_DEFAULT_N_RESULTS", 5),
    }


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


@MCP.tool()
def health_check() -> dict[str, Any]:
    """Return MCP server settings and Python runtime details."""
    settings = _settings()
    return {
        "ok": True,
        "server": "chroma-code-rag-retriever",
        "python": sys.executable,
        "settings": settings,
    }


@MCP.tool()
def search_code_index(
    question: str,
    file: str = "",
    n_results: int = 5,
) -> dict[str, Any]:
    """Search the local Chroma code index for chunks relevant to a question.

    Args:
        question: Natural-language coding question to search for.
        file: Optional file path to restrict retrieval to one indexed file.
        n_results: Number of matching chunks to return.
    """
    settings = _settings()
    selected_file = file.strip() or settings["default_file"] or None
    result_count = n_results if n_results > 0 else settings["default_n_results"]

    matches = run_code_index_search(
        question=question,
        db_path=settings["db_path"],
        collection_name=settings["collection"],
        model=settings["model"],
        base_url=settings["ollama_base_url"],
        n_results=result_count,
        file=selected_file,
    )

    return {
        "ok": True,
        "question": question,
        "file_filter": selected_file,
        "match_count": len(matches),
        "matches": [
            {
                "rank": match.rank,
                "source_path": match.source_path,
                "start_line": match.start_line,
                "end_line": match.end_line,
                "similarity": round(match.similarity, 4),
                "chroma_distance": round(match.chroma_distance, 4),
                "text": match.text,
            }
            for match in matches
        ],
        "continue_context": format_matches_for_continue(question, matches),
    }


@MCP.tool()
def read_code_lines(file: str, start_line: int, end_line: int) -> dict[str, Any]:
    """Read a small line range from a repo file without loading the full file."""
    path = _safe_repo_path(file)
    if not path.exists() or not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    if start_line < 1 or end_line < start_line:
        raise ValueError("Use 1-based line numbers with end_line >= start_line")

    lines = _read_lines(path)
    selected = lines[start_line - 1 : end_line]
    return {
        "ok": True,
        "source_path": str(path.relative_to(_repo_root())),
        "start_line": start_line,
        "end_line": min(end_line, len(lines)),
        "total_lines": len(lines),
        "text": "".join(selected),
    }


@MCP.tool()
def replace_code_lines(
    file: str,
    start_line: int,
    end_line: int,
    replacement: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Replace a line range in a repo file.

    Defaults to dry_run=True so Continue can preview the diff first. Call again
    with dry_run=False only after the diff is correct.
    """
    path = _safe_repo_path(file)
    if not path.exists() or not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    if start_line < 1 or end_line < start_line:
        raise ValueError("Use 1-based line numbers with end_line >= start_line")

    lines = _read_lines(path)
    if end_line > len(lines):
        raise ValueError(f"end_line {end_line} exceeds file length {len(lines)}")

    replacement_text = replacement
    if replacement_text and not replacement_text.endswith("\n"):
        replacement_text += "\n"
    replacement_lines = replacement_text.splitlines(keepends=True)

    before = lines[: start_line - 1]
    after = lines[end_line:]
    updated_lines = before + replacement_lines + after

    rel_path = str(path.relative_to(_repo_root()))
    diff = "".join(
        difflib.unified_diff(
            lines,
            updated_lines,
            fromfile=f"{rel_path} before",
            tofile=f"{rel_path} after",
            lineterm="",
        )
    )

    if not dry_run:
        path.write_text("".join(updated_lines), encoding="utf-8")

    return {
        "ok": True,
        "dry_run": dry_run,
        "source_path": rel_path,
        "start_line": start_line,
        "end_line": end_line,
        "replacement_line_count": len(replacement_lines),
        "diff": diff,
    }


@MCP.tool()
def replace_text_once(
    file: str,
    old_text: str,
    new_text: str,
    start_line: int = 1,
    end_line: int = 0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Replace one exact text snippet in a repo file without loading it in Continue.

    Use this for small edits in files that exceed the model context limit. The
    optional 1-based start_line/end_line range narrows the search area. Defaults
    to dry_run=True so Continue can preview the diff first.
    """
    path = _safe_repo_path(file)
    if not path.exists() or not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    if not old_text:
        raise ValueError("old_text is required")
    if start_line < 1:
        raise ValueError("start_line must be 1 or greater")

    lines = _read_lines(path)
    if end_line == 0:
        end_line = len(lines)
    if end_line < start_line or end_line > len(lines):
        raise ValueError(f"Invalid line range {start_line}-{end_line}")

    before = lines[: start_line - 1]
    selected = "".join(lines[start_line - 1 : end_line])
    after = lines[end_line:]

    count = selected.count(old_text)
    if count != 1:
        raise ValueError(
            f"Expected exactly one old_text match in range, found {count}. "
            "Narrow the line range or use replace_code_lines."
        )

    updated_selected = selected.replace(old_text, new_text, 1)
    updated_text = "".join(before) + updated_selected + "".join(after)
    original_text = "".join(lines)

    rel_path = str(path.relative_to(_repo_root()))
    diff = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=f"{rel_path} before",
            tofile=f"{rel_path} after",
            n=4,
            lineterm="",
        )
    )

    if not dry_run:
        path.write_text(updated_text, encoding="utf-8")

    return {
        "ok": True,
        "dry_run": dry_run,
        "source_path": rel_path,
        "start_line": start_line,
        "end_line": end_line,
        "matches_replaced": 1,
        "diff": diff,
    }


def main() -> None:
    MCP.run(transport="stdio")


if __name__ == "__main__":
    main()
