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


def _to_bare_name(name: str) -> str:
    """
    Strip a leading 'ClassName.' qualifier so that Neo4j qualified names
    (e.g. 'TForm1.Button2Click') resolve to bare names ('Button2Click')
    that match the index.
    """
    s = (name or "").strip()
    if "." in s:
        return s.rsplit(".", 1)[-1].strip()
    return s


def _derive_project_prefix(
    root_file: str,
    index_file_paths: list[str],
) -> str:
    """
    Derive the project directory prefix from the Neo4j root_file path
    by matching it against paths stored in the local index.

    Neo4j stores absolute paths like:
        /mnt/storage/.../011-MultiLevelList/src/Unit1.dfm
    The index stores relative paths like:
        011-MultiLevelList\\src\\Unit1.pas

    Strategy: extract the filename from root_file, find matching index
    paths, and return their common top-level directory.
    """
    if not root_file:
        return ""

    # Normalize and extract filename
    normalized = root_file.replace("\\", "/").strip()
    root_filename = normalized.rsplit("/", 1)[-1].upper()

    # Find index paths whose filename matches the root file
    candidates: list[str] = []
    for fp in index_file_paths:
        fp_norm = fp.replace("\\", "/")
        fp_filename = fp_norm.rsplit("/", 1)[-1].upper()
        if fp_filename == root_filename:
            candidates.append(fp_norm)

    if not candidates:
        # Try matching without extension (dfm → pas, etc.)
        root_stem = root_filename.rsplit(".", 1)[0] if "." in root_filename else root_filename
        for fp in index_file_paths:
            fp_norm = fp.replace("\\", "/")
            fp_filename = fp_norm.rsplit("/", 1)[-1].upper()
            fp_stem = fp_filename.rsplit(".", 1)[0] if "." in fp_filename else fp_filename
            if fp_stem == root_stem:
                candidates.append(fp_norm)

    if not candidates:
        return ""

    # Extract the top-level directory (project prefix) from the first match
    first = candidates[0]
    parts = first.split("/")
    if len(parts) >= 2:
        return parts[0]
    return ""


def _path_matches_prefix(file_path: str, prefix: str) -> bool:
    """Check if a file path belongs to the given project prefix."""
    if not prefix:
        return True
    normalized = file_path.replace("\\", "/")
    return normalized.upper().startswith(prefix.upper())


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
        neo4j_hint_files: set[str] | None = None,
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
        all_functions = self._get_all_functions_with_class()

        # Derive project prefix from root_file to scope all lookups.
        # This prevents cross-project contamination when the index spans
        # multiple projects (e.g. 011-MultiLevelList vs 015-MVC-En-Delphi).
        all_file_paths = [fp for _, fp, _ in all_functions]
        project_prefix = _derive_project_prefix(root_file, all_file_paths)
        if project_prefix:
            logger.info(
                "Scoping Agent 2 to project prefix '%s' (from root_file '%s')",
                project_prefix, root_file,
            )
        if neo4j_hint_files:
            logger.info(
                "Agent 1 provided %d hint files for scope constraint",
                len(neo4j_hint_files),
            )

        # Build edge_map filtered to the project scope
        edge_map: dict[str, list[dict]] = {}
        skipped_cross_project = 0
        for e in all_edges:
            raw_caller = e.get("from") or e.get("caller", "")
            raw_callee = e.get("to") or e.get("callee", "")
            caller_file, caller_class, caller_name = _parse_chunk_id(raw_caller)
            callee_file, callee_class, callee_name = _parse_chunk_id(raw_callee)
            if not caller_name:
                caller_name = _extract_func_name(raw_caller)
            if not callee_name:
                callee_name = _extract_func_name(raw_callee)

            # Skip edges whose caller is outside the project scope
            if project_prefix and not _path_matches_prefix(caller_file, project_prefix):
                skipped_cross_project += 1
                continue

            key = caller_name.upper().strip()
            edge_entry = {
                "caller_name": caller_name,
                "callee_name": callee_name,
                "caller_file": caller_file,
                "callee_file": callee_file,
                "caller_class": caller_class,
                "callee_class": callee_class,
                "caller_raw": raw_caller,
                "callee_raw": raw_callee,
                "confidence": e.get("confidence", 0.8),
            }
            edge_map.setdefault(key, []).append(edge_entry)
            # Also register under "Class.Method" so traversal from a qualified
            # Neo4j root (e.g. TForm1.Button2Click) can find outgoing edges
            if caller_class:
                qualified_key = f"{caller_class.upper().strip()}.{key}"
                if qualified_key != key:
                    edge_map.setdefault(qualified_key, []).append(edge_entry)

        if skipped_cross_project:
            logger.info(
                "Filtered out %d cross-project edges (kept %d in-scope)",
                skipped_cross_project,
                sum(len(v) for v in edge_map.values()),
            )

        # Build function lookups scoped to the same project.
        # We index by BOTH bare name and "Class.Method" qualified name so that
        # lookups from Neo4j (which may use either form) succeed.
        func_to_file: dict[str, str] = {}
        func_to_class: dict[str, str] = {}
        for fn, fp, cn in all_functions:
            if project_prefix and not _path_matches_prefix(fp, project_prefix):
                continue
            key = fn.upper().strip()
            func_to_file[key] = fp
            func_to_class[key] = cn or ""
            # Also register under "CLASS.FUNC" so lookups from Neo4j work
            if cn:
                qualified_key = f"{cn.upper().strip()}.{key}"
                if qualified_key not in func_to_file:
                    func_to_file[qualified_key] = fp
                    func_to_class[qualified_key] = cn

        # Resolve the effective root: try original name, then bare name,
        # then class fallback.  Neo4j may send a qualified root like
        # "TForm1.Button2Click" while the index has bare "Button2Click".
        original_root = root_function
        root_key = root_function.upper().strip()
        root_in_index = root_key in func_to_file
        root_in_edge_map = root_key in edge_map
        resolved_via = "original"

        # Try stripping a ClassName. prefix if the qualified form isn't found
        if not root_in_index and not root_in_edge_map:
            bare_root = _to_bare_name(root_function)
            bare_key = bare_root.upper().strip()
            if bare_key != root_key and (bare_key in func_to_file or bare_key in edge_map):
                logger.info(
                    "Root '%s' not in index; bare name '%s' found — using bare root",
                    root_function, bare_root,
                )
                root_function = bare_root
                root_key = bare_key
                root_in_index = root_key in func_to_file
                root_in_edge_map = root_key in edge_map
                resolved_via = f"bare_name ({original_root} → {bare_root})"

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
            project_prefix=project_prefix,
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
                    project_prefix=project_prefix,
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
                "index_functions": len(func_to_file),
                "index_edges": sum(len(v) for v in edge_map.values()),
                "total_index_functions": len(all_functions),
                "total_index_edges": len(all_edges),
                "project_prefix": project_prefix,
                "skipped_cross_project_edges": skipped_cross_project,
                "neo4j_hint_files": sorted(neo4j_hint_files)[:20] if neo4j_hint_files else [],
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
        project_prefix: str = "",
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
        callee_order = 0
        for e in outgoing:
            callee_name = e["callee_name"]
            callee_key = callee_name.upper().strip()

            # Skip self-referencing edges (forward decl -> implementation of same func)
            if callee_key == key:
                continue

            # Prefer project-scoped func_to_file over the edge's callee_file,
            # since the edge may reference a cross-project file due to name
            # collisions at index time.
            callee_file = func_to_file.get(callee_key, "") or e.get("callee_file", "")
            callee_class = func_to_class.get(callee_key, "") or e.get("callee_class", "")

            # If the resolved callee file is outside our project, skip it
            if project_prefix and callee_file and not _path_matches_prefix(callee_file, project_prefix):
                if callee_name not in unresolved:
                    unresolved.append(callee_name)
                continue

            if not callee_file and callee_name not in unresolved:
                unresolved.append(callee_name)
                continue

            callee_order += 1
            edges.append(
                CallGraphEdge(
                    caller=function_name,
                    callee=callee_name,
                    caller_file=caller_file,
                    callee_file=callee_file,
                    caller_class=caller_class,
                    callee_class=callee_class,
                    depth=depth,
                    execution_order=callee_order,
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
                project_prefix,
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
