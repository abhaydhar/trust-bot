"""
Build call graphs from code chunks using LLM-based extraction and DFM metadata.

Strategy:
  1. LLM extraction — send each code chunk to an LLM which understands language
     semantics and returns only genuine calls (no false positives from variable
     declarations, type references, or uses clauses).  Falls back to minimal
     regex (parenthesised calls only) on LLM failure.
  2. DFM form-to-handler edges — deterministic, metadata-driven (unchanged).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List

from trustbot.indexing.chunker import CodeChunk

logger = logging.getLogger("trustbot.indexing.callgraph")


class CallGraphEdge:
    """Represents a call relationship between two chunks."""

    def __init__(self, from_chunk: str, to_chunk: str, confidence: float = 1.0):
        self.from_chunk = from_chunk
        self.to_chunk = to_chunk
        self.confidence = confidence


def _common_prefix_length(path_a: str, path_b: str) -> int:
    """Count shared leading path components between two file paths."""
    a_parts = path_a.replace("\\", "/").upper().split("/")
    b_parts = path_b.replace("\\", "/").upper().split("/")
    common = 0
    for pa, pb in zip(a_parts, b_parts):
        if pa == pb:
            common += 1
        else:
            break
    return common


def _file_stem(path: str) -> str:
    """Extract the uppercase filename stem (no extension) from a path."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0].upper() if "." in name else name.upper()


def _resolve_callee(
    callee_name: str,
    func_to_chunks: dict[str, list[CodeChunk]],
    caller_file: str,
) -> CodeChunk | None:
    """
    Resolve a callee name to a CodeChunk, preferring chunks in the same
    directory/project as the caller.

    Scoring: (common_prefix_length, stem_match).  The stem_match tie-breaker
    ensures that DFM-to-handler resolution works correctly — Unit1.dfm
    prefers handlers in Unit1.pas over identically-named functions in other
    files (e.g. uMainPourAffichage.pas), even though neither shares a
    common directory prefix.
    """
    candidates = func_to_chunks.get(callee_name.upper())
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    caller_stem = _file_stem(caller_file)
    best = max(
        candidates,
        key=lambda c: (
            _common_prefix_length(c.file_path, caller_file),
            1 if _file_stem(c.file_path) == caller_stem else 0,
        ),
    )
    return best


async def build_call_graph_from_chunks(
    chunks: List[CodeChunk],
    cache_conn: sqlite3.Connection | None = None,
) -> List[CallGraphEdge]:
    """
    Build a call graph from code chunks using:
      1. LLM-based call extraction (with regex fallback per chunk on failure)
      2. DFM form-to-handler edges from metadata

    Parameters
    ----------
    chunks : list of CodeChunk
        All chunks from the codebase.
    cache_conn : sqlite3.Connection, optional
        SQLite connection for the LLM call cache table.  Pass
        ``code_index.get_cache_conn()`` to enable caching.
    """
    from trustbot.indexing.llm_call_extractor import extract_calls_llm

    edges: list[CallGraphEdge] = []

    func_to_chunks: dict[str, list[CodeChunk]] = {}
    for chunk in chunks:
        if chunk.function_name and chunk.function_name != "<module>":
            key = chunk.function_name.upper()
            func_to_chunks.setdefault(key, []).append(chunk)

    def _add_edge(from_id: str, to_id: str, conf: float):
        edges.append(CallGraphEdge(from_chunk=from_id, to_chunk=to_id, confidence=conf))

    # ------------------------------------------------------------------
    # Strategy 1: LLM-based call extraction (replaces old regex + bare-id)
    # ------------------------------------------------------------------
    known_names = [
        func_to_chunks[k][0].function_name
        for k in func_to_chunks
    ]

    chunk_id_to_file: dict[str, str] = {
        c.chunk_id: c.file_path for c in chunks
    }

    llm_edges = await extract_calls_llm(chunks, known_names, cache_conn)

    for caller_chunk_id, callee_name, confidence in llm_edges:
        caller_file = chunk_id_to_file.get(caller_chunk_id, caller_chunk_id)
        callee_chunk = _resolve_callee(callee_name, func_to_chunks, caller_file)
        if callee_chunk:
            _add_edge(caller_chunk_id, callee_chunk.chunk_id, confidence)

    llm_edge_count = len(edges)

    # ------------------------------------------------------------------
    # Strategy 2: DFM form-to-handler edges (deterministic, unchanged)
    # ------------------------------------------------------------------
    dfm_edge_count = 0
    for chunk in chunks:
        handlers = chunk.metadata.get("event_handlers", [])
        if not handlers:
            continue
        for handler_name in handlers:
            target = _resolve_callee(handler_name, func_to_chunks, chunk.file_path)
            if target and target.chunk_id != chunk.chunk_id:
                _add_edge(chunk.chunk_id, target.chunk_id, 0.90)
                dfm_edge_count += 1

    logger.info(
        "Built call graph: %d edges from %d chunks "
        "(%d LLM-extracted, %d DFM form-to-handler)",
        len(edges), len(chunks), llm_edge_count, dfm_edge_count,
    )
    return edges


def build_call_graph_from_chunks_sync(chunks: List[CodeChunk]) -> List[CallGraphEdge]:
    """
    Synchronous fallback that uses only DFM metadata edges (no LLM, no regex).
    Use when an event loop is not available or LLM is explicitly disabled.
    """
    edges: list[CallGraphEdge] = []

    func_to_chunks: dict[str, list[CodeChunk]] = {}
    for chunk in chunks:
        if chunk.function_name and chunk.function_name != "<module>":
            key = chunk.function_name.upper()
            func_to_chunks.setdefault(key, []).append(chunk)

    seen_edges: set[tuple[str, str]] = set()
    dfm_edge_count = 0

    for chunk in chunks:
        handlers = chunk.metadata.get("event_handlers", [])
        if not handlers:
            continue
        for handler_name in handlers:
            target = _resolve_callee(handler_name, func_to_chunks, chunk.file_path)
            if target and target.chunk_id != chunk.chunk_id:
                edge_key = (chunk.chunk_id, target.chunk_id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(CallGraphEdge(
                        from_chunk=chunk.chunk_id,
                        to_chunk=target.chunk_id,
                        confidence=0.90,
                    ))
                    dfm_edge_count += 1

    logger.info(
        "Built call graph (sync/DFM-only): %d edges from %d chunks",
        len(edges), len(chunks),
    )
    return edges
