"""
Knowledge Graph Coverage Agent — compares Neo4j node inventory against
the indexed codebase to find files and functions not present in the KG.

Auto-discovers all Neo4j node labels for a project/run, builds a full
inventory grouped by file and type, then checks function-level coverage
against the SQLite code_index.

No LLM is needed — this is a pure set-comparison agent.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

from trustbot.models.agentic import normalize_file_path
from trustbot.models.kg_coverage import (
    FileCoverage,
    FileKGInventory,
    FunctionCoverage,
    KGCoverageResult,
    Neo4jNodeInfo,
    NodeTypeCount,
)

logger = logging.getLogger("trustbot.agents.kg_coverage")

ProgressCallback = Callable[[float, str], None] | None


def _normalize(name: str) -> str:
    return (name or "").strip().upper()


def _bare_name(name: str) -> str:
    """Strip leading ClassName. prefix for matching."""
    s = _normalize(name)
    if "." in s:
        return s.rsplit(".", 1)[-1].strip()
    return s


class KGCoverageAgent:
    """Compares Neo4j KG inventory against the indexed codebase."""

    def __init__(self, neo4j_tool, code_index) -> None:
        self._neo4j = neo4j_tool
        self._index = code_index

    async def analyze(
        self,
        project_id: int,
        run_id: int,
        progress_callback: ProgressCallback = None,
    ) -> KGCoverageResult:

        def _progress(pct: float, msg: str):
            if progress_callback:
                progress_callback(pct, msg)

        # -- Step 1: Auto-discover node labels --
        _progress(0.05, "Discovering Neo4j node types...")
        label_counts = await self._discover_labels(project_id, run_id)
        logger.info("Discovered %d node labels for project=%d, run=%d",
                     len(label_counts), project_id, run_id)

        # -- Step 2: Fetch all nodes per label --
        _progress(0.15, f"Fetching nodes for {len(label_counts)} label(s)...")
        all_nodes: list[Neo4jNodeInfo] = []
        for label, count in label_counts.items():
            nodes = await self._fetch_nodes_for_label(label, project_id, run_id)
            all_nodes.extend(nodes)
            logger.info("  %s: %d nodes fetched", label, len(nodes))

        total_neo4j = len(all_nodes)
        _progress(0.35, f"Fetched {total_neo4j} total Neo4j nodes.")

        # -- Step 3: Build KG inventory --
        _progress(0.40, "Building KG inventory...")
        node_type_summary = [
            NodeTypeCount(label=lbl, count=cnt)
            for lbl, cnt in sorted(label_counts.items(), key=lambda x: -x[1])
        ]

        files_map: dict[str, list[Neo4jNodeInfo]] = defaultdict(list)
        for node in all_nodes:
            fn = normalize_file_path(node.file_path or node.file_name or "")
            if fn:
                files_map[fn].append(node)

        files_in_neo4j: list[FileKGInventory] = []
        for norm_file, nodes in sorted(files_map.items()):
            counts: dict[str, int] = defaultdict(int)
            for n in nodes:
                counts[n.label] += 1
            raw_path = nodes[0].file_path if nodes else ""
            raw_name = nodes[0].file_name if nodes else norm_file
            files_in_neo4j.append(FileKGInventory(
                file_name=raw_name,
                file_path=raw_path,
                node_counts=dict(counts),
                total_nodes=len(nodes),
                nodes=nodes,
            ))

        # -- Step 4: Load codebase functions --
        _progress(0.50, "Loading codebase functions...")
        codebase_funcs = self._load_codebase_functions()
        logger.info("Codebase has %d functions", len(codebase_funcs))

        # -- Step 5: Compare function-level coverage --
        _progress(0.60, "Comparing coverage...")

        neo4j_lookup = self._build_neo4j_lookup(all_nodes)

        files_by_path: dict[str, list[dict]] = defaultdict(list)
        for func in codebase_funcs:
            norm_file = normalize_file_path(func["file_path"])
            files_by_path[norm_file].append(func)

        file_coverages: list[FileCoverage] = []
        total_covered = 0
        total_uncovered = 0

        for norm_file, funcs in sorted(files_by_path.items()):
            func_results: list[FunctionCoverage] = []
            covered_count = 0

            for func in funcs:
                match = self._match_function(
                    func["function_name"],
                    func["class_name"],
                    func["file_path"],
                    neo4j_lookup,
                )
                func_results.append(match)
                if match.status == "covered":
                    covered_count += 1

            uncovered_count = len(funcs) - covered_count
            total_covered += covered_count
            total_uncovered += uncovered_count

            if covered_count == len(funcs):
                file_status = "covered"
            elif covered_count > 0:
                file_status = "partial"
            else:
                file_status = "uncovered"

            raw_path = funcs[0]["file_path"] if funcs else ""
            raw_name = raw_path.replace("\\", "/").rsplit("/", 1)[-1] if raw_path else norm_file
            language = funcs[0].get("language", "") if funcs else ""

            file_coverages.append(FileCoverage(
                file_path=raw_path,
                file_name=raw_name,
                language=language,
                total_functions=len(funcs),
                covered_functions=covered_count,
                uncovered_functions=uncovered_count,
                status=file_status,
                functions=func_results,
            ))

        total_files = len(file_coverages)
        covered_files = sum(1 for f in file_coverages if f.status == "covered")
        partial_files = sum(1 for f in file_coverages if f.status == "partial")
        uncovered_files = sum(1 for f in file_coverages if f.status == "uncovered")
        total_funcs = total_covered + total_uncovered

        _progress(0.90, "Building result...")

        result = KGCoverageResult(
            project_id=project_id,
            run_id=run_id,
            node_type_summary=node_type_summary,
            total_neo4j_nodes=total_neo4j,
            files_in_neo4j=files_in_neo4j,
            total_codebase_files=total_files,
            covered_files=covered_files,
            partial_files=partial_files,
            uncovered_files=uncovered_files,
            total_codebase_functions=total_funcs,
            covered_functions=total_covered,
            uncovered_functions=total_uncovered,
            file_coverage_pct=(covered_files + partial_files) / max(total_files, 1) * 100,
            function_coverage_pct=total_covered / max(total_funcs, 1) * 100,
            file_coverages=file_coverages,
        )

        logger.info(
            "KG Coverage: %d/%d files covered (%.0f%%), %d/%d functions covered (%.0f%%)",
            covered_files + partial_files, total_files, result.file_coverage_pct,
            total_covered, total_funcs, result.function_coverage_pct,
        )

        _progress(1.0, "Done!")
        return result

    # ------------------------------------------------------------------
    # Neo4j queries
    # ------------------------------------------------------------------

    async def _discover_labels(
        self, project_id: int, run_id: int,
    ) -> dict[str, int]:
        """Auto-discover all node labels present for this project/run."""
        cypher = """
        MATCH (n {project_id: $pid, run_id: $rid})
        RETURN labels(n) AS labels, count(n) AS cnt
        """
        rows = await self._neo4j.query(cypher, {"pid": project_id, "rid": run_id})
        label_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            for lbl in (row.get("labels") or []):
                label_counts[lbl] += row.get("cnt", 0)
        return dict(label_counts)

    async def _fetch_nodes_for_label(
        self, label: str, project_id: int, run_id: int,
    ) -> list[Neo4jNodeInfo]:
        cypher = f"""
        MATCH (n:`{label}` {{project_id: $pid, run_id: $rid}})
        RETURN n.key AS key, n.function_name AS function_name,
               n.name AS name, n.class_name AS class_name,
               n.file_path AS file_path, n.file_name AS file_name,
               n.type AS type
        """
        rows = await self._neo4j.query(cypher, {"pid": project_id, "rid": run_id})
        nodes = []
        for row in rows:
            nodes.append(Neo4jNodeInfo(
                key=row.get("key") or "",
                label=label,
                function_name=row.get("function_name") or "",
                name=row.get("name") or "",
                class_name=row.get("class_name") or "",
                file_path=row.get("file_path") or "",
                file_name=row.get("file_name") or "",
                node_type=row.get("type") or "",
            ))
        return nodes

    # ------------------------------------------------------------------
    # Codebase loading
    # ------------------------------------------------------------------

    def _load_codebase_functions(self) -> list[dict]:
        conn = self._index._get_conn()
        rows = conn.execute(
            "SELECT function_name, file_path, class_name, language FROM code_index"
        ).fetchall()
        return [
            {
                "function_name": r[0],
                "file_path": r[1],
                "class_name": r[2] or "",
                "language": r[3] if len(r) > 3 else "",
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _build_neo4j_lookup(
        self, nodes: list[Neo4jNodeInfo],
    ) -> dict[str, list[Neo4jNodeInfo]]:
        """Build normalized function_name -> [nodes] lookup across all labels."""
        lookup: dict[str, list[Neo4jNodeInfo]] = defaultdict(list)
        for node in nodes:
            fname = node.function_name or node.name
            if not fname:
                continue
            norm = _normalize(fname)
            lookup[norm].append(node)
            bare = _bare_name(fname)
            if bare != norm:
                lookup[bare].append(node)
        return dict(lookup)

    def _match_function(
        self,
        function_name: str,
        class_name: str,
        file_path: str,
        neo4j_lookup: dict[str, list[Neo4jNodeInfo]],
    ) -> FunctionCoverage:
        norm_func = _normalize(function_name)
        bare_func = _bare_name(function_name)
        norm_file = normalize_file_path(file_path)
        norm_class = _normalize(class_name)

        candidates = neo4j_lookup.get(norm_func, [])
        if not candidates:
            candidates = neo4j_lookup.get(bare_func, [])

        if not candidates:
            return FunctionCoverage(
                function_name=function_name,
                file_path=file_path,
                class_name=class_name,
                status="uncovered",
                match_tier="none",
                matched_node_label="",
            )

        # Tier 1: full match (function + class + file)
        for node in candidates:
            n_file = normalize_file_path(node.file_path or node.file_name or "")
            n_class = _normalize(node.class_name)
            if n_file == norm_file and norm_file and n_class == norm_class and norm_class:
                return FunctionCoverage(
                    function_name=function_name,
                    file_path=file_path,
                    class_name=class_name,
                    status="covered",
                    match_tier="full",
                    matched_node_label=node.label,
                )

        # Tier 2: name + file match (class ignored)
        for node in candidates:
            n_file = normalize_file_path(node.file_path or node.file_name or "")
            if n_file == norm_file and norm_file:
                return FunctionCoverage(
                    function_name=function_name,
                    file_path=file_path,
                    class_name=class_name,
                    status="covered",
                    match_tier="name_file",
                    matched_node_label=node.label,
                )

        # Tier 3: name-only match
        return FunctionCoverage(
            function_name=function_name,
            file_path=file_path,
            class_name=class_name,
            status="covered",
            match_tier="name_only",
            matched_node_label=candidates[0].label,
        )
