"""
Agent 2 — Indexed Codebase Graph Builder.

Builds a call graph from the indexed codebase (populated via the Code Indexer
tab). Takes the ROOT Snippet identified by Agent 1, looks it up in the
code index, and traverses the stored call edges to construct an independent
call graph for comparison.

NO access to Neo4j — operates purely on the local index (SQLite).
"""

from __future__ import annotations

import logging
from datetime import datetime

from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import (
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
)

logger = logging.getLogger("trustbot.agents.agent2_index")


def _parse_chunk_id(chunk_id: str) -> tuple[str, str, str]:
    """
    Parse a chunk ID into (file_path, class_name, function_name).
    Chunk IDs look like:  path/file.pas::::FuncName
                      or:  path/file.pas::ClassName::FuncName
    """
    parts = chunk_id.split("::")
    file_path = parts[0].strip() if len(parts) >= 1 else ""
    class_name = parts[1].strip() if len(parts) >= 3 else ""
    func_name = ""
    for part in reversed(parts):
        stripped = part.strip()
        if stripped:
            func_name = stripped
            break
    if func_name == file_path:
        func_name = ""
    return file_path, class_name, func_name


def _extract_func_name(chunk_id: str) -> str:
    """Extract just the bare function name from a chunk ID."""
    _, _, func_name = _parse_chunk_id(chunk_id)
    return func_name or chunk_id.strip()


class Agent2IndexBuilder:
    """
    Agent that builds a call graph from the indexed codebase.

    Uses the CodeIndex (SQLite) which was populated when the user cloned
    and indexed a Git repository via the Code Indexer tab.
    """

    def __init__(self, code_index: CodeIndex) -> None:
        self._index = code_index

    async def build(
        self,
        root_function: str,
        execution_flow_id: str = "",
    ) -> CallGraphOutput:
        """
        Build a call graph starting from root_function by traversing
        the indexed call edges.
        """
        edges: list[CallGraphEdge] = []
        unresolved: list[str] = []
        visited: set[str] = set()

        all_edges = self._index.get_edges()

        # Build edge map keyed by BARE function name (uppercase).
        # Each entry stores parsed caller/callee info including class and file.
        edge_map: dict[str, list[dict]] = {}
        for e in all_edges:
            raw_caller = e.get("from") or e.get("caller", "")
            raw_callee = e.get("to") or e.get("callee", "")
            caller_file, caller_class, caller_name = _parse_chunk_id(raw_caller)
            callee_file, callee_class, callee_name = _parse_chunk_id(raw_callee)
            if not caller_name:
                caller_name = _extract_func_name(raw_caller)
            if not callee_name:
                callee_name = _extract_func_name(raw_callee)
            key = caller_name.upper().strip()
            edge_map.setdefault(key, []).append({
                "caller_name": caller_name,
                "callee_name": callee_name,
                "caller_file": caller_file,
                "callee_file": callee_file,
                "caller_class": caller_class,
                "callee_class": callee_class,
                "caller_raw": raw_caller,
                "callee_raw": raw_callee,
                "confidence": e.get("confidence", 0.8),
            })

        all_functions = self._get_all_functions_with_class()
        func_to_file: dict[str, str] = {}
        func_to_class: dict[str, str] = {}
        for fn, fp, cn in all_functions:
            key = fn.upper().strip()
            func_to_file[key] = fp
            func_to_class[key] = cn or ""

        root_key = root_function.upper().strip()
        root_in_index = root_key in func_to_file
        root_in_edge_map = root_key in edge_map
        root_outgoing = len(edge_map.get(root_key, []))

        if not root_in_index:
            logger.warning(
                "Root function '%s' NOT FOUND in index (%d functions). "
                "Sample index functions: %s",
                root_function, len(all_functions),
                list(func_to_file.keys())[:10],
            )
        if not root_in_edge_map:
            logger.warning(
                "Root function '%s' has NO OUTGOING EDGES in index (%d total edges). "
                "Sample edge_map keys: %s",
                root_function, len(all_edges),
                list(edge_map.keys())[:10],
            )

        self._traverse(
            root_function,
            edge_map,
            func_to_file,
            func_to_class,
            edges,
            unresolved,
            visited,
            depth=1,
        )

        # Diagnostic metadata for debugging matching issues
        sample_index_funcs = sorted(func_to_file.keys())[:15]
        sample_edge_callers = sorted(edge_map.keys())[:15]

        output = CallGraphOutput(
            execution_flow_id=execution_flow_id,
            source=GraphSource.FILESYSTEM,
            root_function=root_function,
            edges=edges,
            unresolved_callees=unresolved,
            metadata={
                "total_depth": max((e.depth for e in edges), default=0),
                "total_nodes": len(
                    set(e.caller for e in edges) | set(e.callee for e in edges)
                ),
                "index_functions": len(all_functions),
                "index_edges": len(all_edges),
                "root_found_in_index": root_in_index,
                "root_has_outgoing_edges": root_in_edge_map,
                "root_outgoing_count": root_outgoing,
                "sample_index_functions": sample_index_funcs,
                "sample_edge_callers": sample_edge_callers,
            },
        )

        logger.info(
            "Agent 2 built %d edges from index for flow %s (root: %s, %d unresolved)",
            len(edges), execution_flow_id, root_function, len(unresolved),
        )
        return output

    def _traverse(
        self,
        function_name: str,
        edge_map: dict[str, list[dict]],
        func_to_file: dict[str, str],
        func_to_class: dict[str, str],
        edges: list[CallGraphEdge],
        unresolved: list[str],
        visited: set[str],
        depth: int,
        max_depth: int = 50,
    ) -> None:
        if depth > max_depth:
            return

        key = function_name.upper().strip()
        if key in visited:
            return
        visited.add(key)

        caller_file = func_to_file.get(key, "")
        caller_class = func_to_class.get(key, "")

        outgoing = edge_map.get(key, [])
        for e in outgoing:
            callee_name = e["callee_name"]
            callee_key = callee_name.upper().strip()
            callee_file = e.get("callee_file") or func_to_file.get(callee_key, "")
            callee_class = e.get("callee_class") or func_to_class.get(callee_key, "")

            if not callee_file and callee_name not in unresolved:
                unresolved.append(callee_name)
                continue

            edges.append(
                CallGraphEdge(
                    caller=function_name,
                    callee=callee_name,
                    caller_file=caller_file,
                    callee_file=callee_file,
                    caller_class=caller_class,
                    callee_class=callee_class,
                    depth=depth,
                    extraction_method=ExtractionMethod.REGEX,
                    confidence=e.get("confidence", 0.8),
                )
            )

            self._traverse(
                callee_name,
                edge_map,
                func_to_file,
                func_to_class,
                edges,
                unresolved,
                visited,
                depth + 1,
                max_depth,
            )

    def _get_all_functions(self) -> list[tuple[str, str]]:
        """Return all (function_name, file_path) pairs from the index."""
        conn = self._index._get_conn()
        cursor = conn.execute("SELECT function_name, file_path FROM code_index")
        return cursor.fetchall()

    def _get_all_functions_with_class(self) -> list[tuple[str, str, str]]:
        """Return all (function_name, file_path, class_name) from the index."""
        conn = self._index._get_conn()
        cursor = conn.execute(
            "SELECT function_name, file_path, class_name FROM code_index"
        )
        return cursor.fetchall()
