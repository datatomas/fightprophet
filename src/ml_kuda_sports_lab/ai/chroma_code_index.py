#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Index selected code files in Chroma using Ollama/Nomic embeddings.

Examples:
    python3 src/ml_kuda_sports_lab/ai/chroma_code_index.py index \
        --file src/ml_kuda_sports_lab/front_end/mma_front_streamlit.py

    python3 src/ml_kuda_sports_lab/ai/chroma_code_index.py query \
        "where does the Streamlit app handle page routing?"

The query output is intentionally formatted so it can be pasted into Continue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text:latest"
DEFAULT_DB_PATH = "data/chroma/code"
DEFAULT_COLLECTION = "ml_kuda_code"
DEFAULT_CHUNK_LINES = 30
DEFAULT_OVERLAP_LINES = 5
DEFAULT_MAX_CHARS = 1_800


@dataclass(frozen=True)
class CodeChunk:
    path: str
    start_line: int
    end_line: int
    text: str

    @property
    def stable_id(self) -> str:
        digest = hashlib.sha1(
            f"{self.path}:{self.start_line}:{self.end_line}:{self.text}".encode("utf-8")
        ).hexdigest()
        return f"{self.path}:{self.start_line}:{self.end_line}:{digest[:12]}"


@dataclass(frozen=True)
class SearchMatch:
    rank: int
    source_path: str
    start_line: int
    end_line: int
    text: str
    similarity: float
    chroma_distance: float


def require_chromadb() -> Any:
    try:
        import chromadb
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: chromadb. Install it in this environment with "
            "`python3 -m pip install chromadb`."
        ) from exc
    return chromadb


def embed_text(text: str, model: str, base_url: str) -> list[float]:
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(
            "Could not reach Ollama. Start/enable the existing Ollama service and "
            "confirm the base URL."
        ) from exc

    vector = body.get("embedding")
    if not isinstance(vector, list) or not vector:
        raise RuntimeError(f"Ollama response did not include an embedding: {body}")
    return [float(value) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root()))
    except ValueError:
        return str(path.resolve())


def chunk_file(
    path: Path,
    chunk_lines: int,
    overlap_lines: int,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[CodeChunk]:
    if chunk_lines <= 0:
        raise ValueError("--chunk-lines must be greater than zero")
    if overlap_lines < 0 or overlap_lines >= chunk_lines:
        raise ValueError("--overlap-lines must be between 0 and chunk-lines - 1")
    if max_chars <= 0:
        raise ValueError("--max-chars must be greater than zero")

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return []

    chunks: list[CodeChunk] = []
    source_path = display_path(path)
    start_index = 0

    while start_index < len(lines):
        end_index = min(start_index + chunk_lines, len(lines))
        chunk_text = "\n".join(lines[start_index:end_index]).strip()

        while len(chunk_text) > max_chars and end_index > start_index + 1:
            end_index -= 1
            chunk_text = "\n".join(lines[start_index:end_index]).strip()

        if chunk_text:
            chunks.append(
                CodeChunk(
                    path=source_path,
                    start_line=start_index + 1,
                    end_line=end_index,
                    text=chunk_text,
                )
            )
        if end_index == len(lines):
            break
        start_index = max(end_index - overlap_lines, start_index + 1)
    return chunks


def get_collection(db_path: str, collection_name: str) -> Any:
    chromadb = require_chromadb()
    client = chromadb.PersistentClient(path=db_path)
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )


def delete_existing_file_chunks(collection: Any, source_path: str) -> int:
    existing = collection.get(where={"source_path": source_path}, include=[])
    ids = existing.get("ids") or []
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def search_code_index(
    *,
    question: str,
    db_path: str,
    collection_name: str,
    model: str,
    base_url: str,
    n_results: int,
    file: str | None = None,
) -> list[SearchMatch]:
    collection = get_collection(db_path, collection_name)
    query_vector = embed_text(question, model=model, base_url=base_url)
    where = {"source_path": display_path(Path(file).expanduser().resolve())} if file else None

    results = collection.query(
        query_embeddings=[query_vector],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "embeddings", "distances"],
    )

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    embeddings = results.get("embeddings", [[]])[0]
    distances = results.get("distances", [[]])[0]

    matches: list[SearchMatch] = []
    for rank, (document, metadata, embedding, distance) in enumerate(
        zip(documents, metadatas, embeddings, distances), start=1
    ):
        similarity = cosine_similarity(query_vector, [float(value) for value in embedding])
        matches.append(
            SearchMatch(
                rank=rank,
                source_path=metadata["source_path"],
                start_line=int(metadata["start_line"]),
                end_line=int(metadata["end_line"]),
                text=document,
                similarity=similarity,
                chroma_distance=float(distance),
            )
        )
    return matches


def format_matches_for_continue(question: str, matches: list[SearchMatch]) -> str:
    if not matches:
        return "No matching chunks found. Index a file first with the `index` command."

    lines = [
        "Paste this context into Continue, then ask your coding question:",
        "",
        f"Question: {question}",
        "",
    ]
    for match in matches:
        lines.extend(
            [
                (
                    f"## Match {match.rank}: {match.source_path}:"
                    f"{match.start_line}-{match.end_line}"
                ),
                f"Similarity: {match.similarity:.4f}",
                f"Chroma distance: {match.chroma_distance:.4f}",
                "```",
                match.text,
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def index_file(args: argparse.Namespace) -> None:
    path = Path(args.file).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise SystemExit(f"File does not exist: {path}")

    chunks = chunk_file(path, args.chunk_lines, args.overlap_lines, args.max_chars)
    if not chunks:
        raise SystemExit(f"No text chunks found in {path}")

    collection = get_collection(args.db_path, args.collection)
    source_path = display_path(path)
    removed = delete_existing_file_chunks(collection, source_path)

    ids: list[str] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks, start=1):
        print(
            f"Embedding chunk {index}/{len(chunks)} "
            f"({chunk.path}:{chunk.start_line}-{chunk.end_line})"
        )
        embeddings.append(
            embed_text(chunk.text, model=args.model, base_url=args.ollama_base_url)
        )
        ids.append(chunk.stable_id)
        documents.append(chunk.text)
        metadatas.append(
            {
                "source_path": chunk.path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "embedding_model": args.model,
            }
        )

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    print()
    print(f"Indexed file: {source_path}")
    print(f"Chunks added: {len(chunks)}")
    print(f"Old chunks removed: {removed}")
    print(f"Chroma path: {args.db_path}")
    print(f"Collection: {args.collection}")


def query_index(args: argparse.Namespace) -> None:
    matches = search_code_index(
        question=args.question,
        db_path=args.db_path,
        collection_name=args.collection,
        model=args.model,
        base_url=args.ollama_base_url,
        n_results=args.n_results,
        file=args.file,
    )
    print(format_matches_for_continue(args.question, matches))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build/query a local Chroma code index with Ollama Nomic embeddings."
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument(
        "--ollama-base-url",
        default=os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Embed and store one selected file.")
    index_parser.add_argument("--file", required=True)
    index_parser.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES)
    index_parser.add_argument("--overlap-lines", type=int, default=DEFAULT_OVERLAP_LINES)
    index_parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    index_parser.set_defaults(func=index_file)

    query_parser = subparsers.add_parser("query", help="Search the saved code index.")
    query_parser.add_argument("question")
    query_parser.add_argument("--file", help="Limit search to one indexed file.")
    query_parser.add_argument("--n-results", type=int, default=5)
    query_parser.set_defaults(func=query_index)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
