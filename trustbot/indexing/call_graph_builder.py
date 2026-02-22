"""
Build call graphs from code chunks using static analysis and LLM.
"""

from __future__ import annotations

import logging
import re
from typing import List

from trustbot.indexing.chunker import CodeChunk

logger = logging.getLogger("trustbot.indexing.callgraph")


class CallGraphEdge:
    """Represents a call relationship between two chunks."""
    
    def __init__(self, from_chunk: str, to_chunk: str, confidence: float = 1.0):
        self.from_chunk = from_chunk
        self.to_chunk = to_chunk
        self.confidence = confidence


SKIP_TOKENS = frozenset({
    # Python / general keywords
    'if', 'for', 'while', 'def', 'class', 'return', 'print', 'len', 'str',
    'int', 'range', 'elif', 'else', 'try', 'except', 'raise', 'with', 'as',
    'import', 'from', 'not', 'and', 'or', 'in', 'is', 'True', 'False', 'None',
    'self', 'cls', 'super', 'type', 'isinstance', 'assert',
    # Delphi / Pascal keywords
    'begin', 'end', 'var', 'const', 'type', 'uses', 'unit', 'interface',
    'implementation', 'program', 'procedure', 'function', 'constructor',
    'destructor', 'property', 'inherited', 'result', 'nil', 'then', 'do',
    'of', 'to', 'downto', 'repeat', 'until', 'case', 'with', 'try', 'finally',
    'except', 'raise', 'exit', 'break', 'continue', 'array', 'record',
    'object', 'set', 'file', 'string', 'integer', 'boolean', 'byte', 'word',
    'cardinal', 'longint', 'double', 'single', 'extended', 'char', 'widechar',
    'sizeof', 'length', 'high', 'low', 'ord', 'chr', 'inc', 'dec', 'new',
    'dispose', 'freemem', 'getmem',
})

# Patterns that capture function/method calls across multiple languages.
# Each pattern must have a named group 'callee'.
CALL_PATTERNS = [
    # Standard: FunctionName(
    re.compile(r'\b(?P<callee>[A-Za-z_]\w*)\s*\('),
    # Delphi dot-notation: Object.Method( or Self.Method( or ClassName.Method(
    re.compile(r'(?:\w+)\.(?P<callee>[A-Za-z_]\w*)\s*\('),
    # Delphi := FunctionName( on right side of assignment
    re.compile(r':=\s*(?P<callee>[A-Za-z_]\w*)\s*\('),
    # Delphi := Object.Method(
    re.compile(r':=\s*\w+\.(?P<callee>[A-Za-z_]\w*)\s*\('),
]


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


def _resolve_callee(
    callee_name: str,
    func_to_chunks: dict[str, list[CodeChunk]],
    caller_file: str,
) -> CodeChunk | None:
    """
    Resolve a callee name to a CodeChunk, preferring chunks in the same
    directory/project as the caller.  When multiple chunks share the same
    function name across projects, pick the one with the longest common
    path prefix with the caller.
    """
    candidates = func_to_chunks.get(callee_name.upper())
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Score by path proximity to caller
    best = max(candidates, key=lambda c: _common_prefix_length(c.file_path, caller_file))
    return best


def build_call_graph_from_chunks(chunks: List[CodeChunk]) -> List[CallGraphEdge]:
    """
    Build a call graph from code chunks using two strategies:

    1. Regex patterns — catches calls with parentheses and dot-notation
    2. Bare identifier matching — for Delphi/Pascal where procedures can be
       called without parentheses (e.g. `ChargeArborescence;`)

    For strategy 2: scan each chunk's body for known function names that appear
    as standalone identifiers (word boundary on both sides). Skip matches that
    are the chunk's own definition.
    """
    edges = []

    # Map uppercase function name -> all chunks with that name (multi-project safe)
    func_to_chunks: dict[str, list[CodeChunk]] = {}
    for chunk in chunks:
        if chunk.function_name and chunk.function_name != "<module>":
            key = chunk.function_name.upper()
            func_to_chunks.setdefault(key, []).append(chunk)

    seen_edges: set[tuple[str, str]] = set()

    def _add_edge(from_chunk: CodeChunk, to_chunk: CodeChunk, conf: float):
        edge_key = (from_chunk.chunk_id, to_chunk.chunk_id)
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            edges.append(CallGraphEdge(
                from_chunk=from_chunk.chunk_id,
                to_chunk=to_chunk.chunk_id,
                confidence=conf,
            ))

    # Strategy 1: regex-based call patterns (with parentheses)
    for chunk in chunks:
        if not chunk.content:
            continue

        for pattern in CALL_PATTERNS:
            for match in pattern.finditer(chunk.content):
                callee_name = match.group("callee")
                if callee_name.lower() in SKIP_TOKENS:
                    continue
                callee_chunk = _resolve_callee(callee_name, func_to_chunks, chunk.file_path)
                if callee_chunk and callee_chunk.chunk_id != chunk.chunk_id:
                    _add_edge(chunk, callee_chunk, 0.75)

    # Strategy 2: bare identifier matching (Delphi parameterless calls).
    delphi_languages = {"delphi", "pascal"}
    delphi_chunks = [c for c in chunks if c.language in delphi_languages and c.content]

    if delphi_chunks:
        # Build patterns for all known function names (min 3 chars to avoid noise)
        bare_patterns: dict[str, re.Pattern] = {}
        all_func_names: set[str] = set()
        for key in func_to_chunks:
            fname = func_to_chunks[key][0].function_name
            if len(fname) >= 3 and fname.lower() not in SKIP_TOKENS:
                bare_patterns[fname] = re.compile(
                    r'\b' + re.escape(fname) + r'\b', re.IGNORECASE,
                )
                all_func_names.add(fname.upper())

        for chunk in delphi_chunks:
            if not chunk.function_name or chunk.function_name == "<module>":
                continue
            for fname, pat in bare_patterns.items():
                if pat.search(chunk.content):
                    target_chunk = _resolve_callee(fname, func_to_chunks, chunk.file_path)
                    if target_chunk and target_chunk.chunk_id != chunk.chunk_id:
                        _add_edge(chunk, target_chunk, 0.65)

    # Strategy 3: .dfm form-to-handler edges.
    # DFM chunks have metadata["event_handlers"] listing handler function names
    # (e.g., Button1Click, FormCreate). Create edges from the form to each handler.
    dfm_edge_count = 0
    for chunk in chunks:
        handlers = chunk.metadata.get("event_handlers", [])
        if not handlers:
            continue
        for handler_name in handlers:
            target = _resolve_callee(handler_name, func_to_chunks, chunk.file_path)
            if target and target.chunk_id != chunk.chunk_id:
                _add_edge(chunk, target, 0.90)
                dfm_edge_count += 1

    logger.info(
        "Built call graph: %d edges from %d chunks "
        "(%d Delphi bare-call scans, %d DFM form-to-handler edges)",
        len(edges), len(chunks), len(delphi_chunks), dfm_edge_count,
    )
    return edges
