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
        root_class: str = "",
        root_file: str = "",
    ) -> CallGraphOutput:
        """
        Build a call graph starting from root_function by traversing
        the indexed call edges.

        Fallback chain when root_function is not in the index:
        1. Try root_function as-is (e.g. "InitialiseEcran")
        2. Try root_class (e.g. "TForm1" — for .dfm form roots)
        3. Try root_function's first callee from Neo4j edges (skip non-code roots)
        """
        edges: list[CallGraphEdge] = []
        unresolved: list[str] = []
        visited: set[str] = set()

        all_edges = self._index.get_edges()

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

        # Resolve the effective root: try original name, then class fallback
        original_root = root_function
        root_key = root_function.upper().strip()
        root_in_index = root_key in func_to_file
        root_in_edge_map = root_key in edge_map
        resolved_via = "original"

        if not root_in_index and not root_in_edge_map and root_class:
            class_key = root_class.upper().strip()
            # Fallback 1: try the class name directly (e.g. TForm1 as a chunk)
            if class_key in func_to_file or class_key in edge_map:
                logger.info(
                    "Root '%s' not found, falling back to class '%s'",
                    root_function, root_class,
                )
                root_function = root_class
                root_key = class_key
                root_in_index = root_key in func_to_file
                root_in_edge_map = root_key in edge_map
                resolved_via = f"class_fallback ({root_class})"
            else:
                # Fallback 2: find all functions belonging to this class and
                # traverse from each of them. For .dfm form roots like Form1/TForm1,
                # this picks up Button2Click, FormCreate, etc. in the class.
                class_members = [
                    fn_key for fn_key, cls in func_to_class.items()
                    if cls.upper().strip() == class_key
                ]
                if class_members:
                    logger.info(
                        "Root '%s' and class '%s' not found as functions. "
                        "Traversing from %d class members: %s",
                        original_root, root_class, len(class_members),
                        class_members[:5],
                    )
                    resolved_via = f"class_members ({root_class} -> {len(class_members)} functions)"
                    root_in_index = True
                    root_in_edge_map = True

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

        # Primary traversal from the resolved root
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

        # If primary traversal found nothing and we have class members, traverse
        # from each member function (handles .dfm form → class member resolution)
        if not edges and root_class and "class_members" in resolved_via:
            class_key = root_class.upper().strip()
            class_members = [
                fn_key for fn_key, cls in func_to_class.items()
                if cls.upper().strip() == class_key
            ]
            for member_key in class_members:
                # Use the original-case function name from the index
                member_name = next(
                    (fn for fn, _, _ in all_functions if fn.upper().strip() == member_key),
                    member_key,
                )
                self._traverse(
                    member_name,
                    edge_map,
                    func_to_file,
                    func_to_class,
                    edges,
                    unresolved,
                    visited,
                    depth=1,
                )

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
                "original_root": original_root,
                "resolved_root": root_function,
                "resolved_via": resolved_via,
                "root_class_hint": root_class,
                "root_file_hint": root_file,
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
