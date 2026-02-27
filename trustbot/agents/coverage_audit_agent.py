"""
Coverage Audit Agent — project-wide Neo4j completeness checker.

Compares ALL CALLS relationships in Neo4j (for a project/run) against ALL
call edges found in the indexed codebase.  Produces a structured result
highlighting calls that exist in the source files but are missing from
Neo4j, plus calls in Neo4j not confirmed by the codebase.

No LLM is needed — this is a pure set-comparison agent.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from trustbot.models.agentic import normalize_file_path

logger = logging.getLogger("trustbot.agents.coverage_audit")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class AuditEdge(BaseModel):
    """A single edge in the audit report."""

    caller: str
    callee: str
    caller_file: str = ""
    callee_file: str = ""
    caller_class: str = ""
    callee_class: str = ""
    confidence: float = 1.0


class CoverageAuditResult(BaseModel):
    """Structured output of the coverage audit."""

    project_id: int
    run_id: int
    neo4j_total_edges: int = 0
    codebase_total_edges: int = 0
    neo4j_snippet_count: int = 0
    codebase_function_count: int = 0
    confirmed: list[AuditEdge] = Field(default_factory=list)
    missing_from_neo4j: list[AuditEdge] = Field(default_factory=list)
    phantom_in_neo4j: list[AuditEdge] = Field(default_factory=list)
    coverage_score: float = 0.0
    metadata: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_chunk_id(chunk_id: str) -> tuple[str, str, str]:
    """Parse chunk_id → (file_path, class_name, function_name)."""
    parts = chunk_id.split("::")
    file_path = parts[0].strip() if parts else ""
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


_EdgeKey = tuple[str, str, str, str]  # (caller_func, caller_file, callee_func, callee_file)


def _normalise_key(caller_func: str, caller_file: str,
                   callee_func: str, callee_file: str) -> _EdgeKey:
    return (
        caller_func.upper().strip(),
        normalize_file_path(caller_file),
        callee_func.upper().strip(),
        normalize_file_path(callee_file),
    )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CoverageAuditAgent:
    """Project-wide comparison of Neo4j CALLS vs indexed codebase edges."""

    def __init__(self, neo4j_tool, code_index) -> None:
        self._neo4j = neo4j_tool
        self._index = code_index

    async def audit(
        self,
        project_id: int,
        run_id: int,
        progress_callback=None,
    ) -> CoverageAuditResult:
        """Run the full audit and return a structured result."""

        # -- Step 1: fetch Neo4j edges --
        if progress_callback:
            progress_callback(0.05, "Fetching Neo4j call graph...")

        project_graph = await self._neo4j.get_project_call_graph(
            project_id, run_id,
        )
        all_snippets = project_graph.all_snippets
        all_neo4j_edges = project_graph.all_edges

        neo4j_set: dict[_EdgeKey, AuditEdge] = {}
        for edge in all_neo4j_edges:
            caller_snip = all_snippets.get(edge.caller_id)
            callee_snip = all_snippets.get(edge.callee_id)
            if not caller_snip or not callee_snip:
                continue
            caller_func = caller_snip.function_name or caller_snip.name or ""
            callee_func = callee_snip.function_name or callee_snip.name or ""
            if not caller_func or not callee_func:
                continue
            key = _normalise_key(
                caller_func, caller_snip.file_path,
                callee_func, callee_snip.file_path,
            )
            if key not in neo4j_set:
                neo4j_set[key] = AuditEdge(
                    caller=caller_func,
                    callee=callee_func,
                    caller_file=caller_snip.file_path,
                    callee_file=callee_snip.file_path,
                    caller_class=caller_snip.class_name,
                    callee_class=callee_snip.class_name,
                    confidence=1.0,
                )

        if progress_callback:
            progress_callback(0.40, "Fetching codebase edges...")

        # -- Step 2: fetch codebase edges --
        raw_edges = self._index.get_edges()

        func_info = self._load_function_info()

        codebase_set: dict[_EdgeKey, AuditEdge] = {}
        for row in raw_edges:
            caller_file, caller_cls, caller_func = _parse_chunk_id(row["from"])
            callee_file, callee_cls, callee_func = _parse_chunk_id(row["to"])
            if not caller_func or not callee_func:
                continue
            key = _normalise_key(caller_func, caller_file,
                                 callee_func, callee_file)
            if key not in codebase_set:
                codebase_set[key] = AuditEdge(
                    caller=caller_func,
                    callee=callee_func,
                    caller_file=caller_file,
                    callee_file=callee_file,
                    caller_class=caller_cls or func_info.get(
                        (caller_func.upper(), normalize_file_path(caller_file)), "",
                    ),
                    callee_class=callee_cls or func_info.get(
                        (callee_func.upper(), normalize_file_path(callee_file)), "",
                    ),
                    confidence=row.get("confidence", 1.0),
                )

        if progress_callback:
            progress_callback(0.70, "Comparing edges...")

        # -- Step 3: set comparison --
        neo4j_keys = set(neo4j_set.keys())
        codebase_keys = set(codebase_set.keys())

        confirmed_keys = neo4j_keys & codebase_keys
        missing_keys = codebase_keys - neo4j_keys
        phantom_keys = neo4j_keys - codebase_keys

        # Also try name-only matching for edges where file paths differ
        # between Neo4j (absolute) and codebase (relative)
        neo4j_name_only: dict[tuple[str, str], _EdgeKey] = {}
        for k in phantom_keys:
            neo4j_name_only[(k[0], k[2])] = k

        remaining_missing: set[_EdgeKey] = set()
        extra_confirmed_neo4j: set[_EdgeKey] = set()
        extra_confirmed_code: set[_EdgeKey] = set()

        for mk in missing_keys:
            name_pair = (mk[0], mk[2])
            if name_pair in neo4j_name_only:
                extra_confirmed_code.add(mk)
                extra_confirmed_neo4j.add(neo4j_name_only[name_pair])
            else:
                remaining_missing.add(mk)

        confirmed_keys |= extra_confirmed_code
        phantom_keys -= extra_confirmed_neo4j
        missing_keys = remaining_missing

        confirmed = [
            codebase_set.get(k) or neo4j_set[k] for k in sorted(confirmed_keys)
        ]
        missing_from_neo4j = sorted(
            [codebase_set[k] for k in missing_keys],
            key=lambda e: (e.caller_file, e.caller, e.callee),
        )
        phantom_in_neo4j = sorted(
            [neo4j_set[k] for k in phantom_keys],
            key=lambda e: (e.caller_file, e.caller, e.callee),
        )

        total_relevant = len(confirmed) + len(missing_from_neo4j)
        coverage = len(confirmed) / max(total_relevant, 1)

        codebase_func_count = self._count_functions()

        if progress_callback:
            progress_callback(0.95, "Building report...")

        result = CoverageAuditResult(
            project_id=project_id,
            run_id=run_id,
            neo4j_total_edges=len(all_neo4j_edges),
            codebase_total_edges=len(raw_edges),
            neo4j_snippet_count=len(all_snippets),
            codebase_function_count=codebase_func_count,
            confirmed=confirmed,
            missing_from_neo4j=missing_from_neo4j,
            phantom_in_neo4j=phantom_in_neo4j,
            coverage_score=coverage,
            metadata={
                "flows_scanned": len(project_graph.execution_flows),
                "name_only_matches": len(extra_confirmed_code),
            },
        )

        logger.info(
            "Coverage audit: %d confirmed, %d missing from Neo4j, "
            "%d phantom in Neo4j (%.0f%% coverage)",
            len(confirmed), len(missing_from_neo4j),
            len(phantom_in_neo4j), coverage * 100,
        )

        if progress_callback:
            progress_callback(1.0, "Done!")

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_function_info(self) -> dict[tuple[str, str], str]:
        """Load (FUNC_NAME, NORM_FILE) → class_name map from code_index."""
        conn = self._index._get_conn()
        rows = conn.execute(
            "SELECT function_name, file_path, class_name FROM code_index"
        ).fetchall()
        info: dict[tuple[str, str], str] = {}
        for r in rows:
            key = (r[0].upper().strip(), normalize_file_path(r[1]))
            info[key] = r[2] or ""
        return info

    def _count_functions(self) -> int:
        conn = self._index._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()
        return row[0] if row else 0
