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

    function_index: dict[str, CodeChunk] = {}
    function_index_upper: dict[str, CodeChunk] = {}
    for chunk in chunks:
        if chunk.function_name and chunk.function_name != "<module>":
            function_index[chunk.function_name] = chunk
            function_index_upper[chunk.function_name.upper()] = chunk

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
                callee_chunk = function_index.get(callee_name)
                if not callee_chunk:
                    callee_chunk = function_index_upper.get(callee_name.upper())
                if callee_chunk and callee_chunk.chunk_id != chunk.chunk_id:
                    _add_edge(chunk, callee_chunk, 0.75)

    # Strategy 2: bare identifier matching (Delphi parameterless calls).
    # For each known function name, build a word-boundary regex and scan
    # all chunks for occurrences. This catches `ChargeArborescence;` without ().
    # Only applied to Delphi/Pascal files to avoid false positives.
    delphi_languages = {"delphi", "pascal"}
    delphi_chunks = [c for c in chunks if c.language in delphi_languages and c.content]

    if delphi_chunks:
        # Build patterns for all known function names (min 3 chars to avoid noise)
        bare_patterns: dict[str, tuple[re.Pattern, CodeChunk]] = {}
        for fname, fchunk in function_index.items():
            if len(fname) >= 3 and fname.lower() not in SKIP_TOKENS:
                bare_patterns[fname] = (
                    re.compile(r'\b' + re.escape(fname) + r'\b', re.IGNORECASE),
                    fchunk,
                )

        for chunk in delphi_chunks:
            if not chunk.function_name or chunk.function_name == "<module>":
                continue
            for fname, (pat, target_chunk) in bare_patterns.items():
                if target_chunk.chunk_id == chunk.chunk_id:
                    continue
                if pat.search(chunk.content):
                    _add_edge(chunk, target_chunk, 0.65)

    logger.info(
        "Built call graph: %d edges from %d chunks (%d Delphi chunks scanned for bare calls)",
        len(edges), len(chunks), len(delphi_chunks),
    )
    return edges
