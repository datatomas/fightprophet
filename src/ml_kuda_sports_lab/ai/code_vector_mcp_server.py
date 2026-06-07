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


def main() -> None:
    MCP.run(transport="stdio")


if __name__ == "__main__":
    main()
