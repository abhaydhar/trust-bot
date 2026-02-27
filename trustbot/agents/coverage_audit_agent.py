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

        # -- Step 1: fetch ALL Neo4j CALLS edges directly (no execution flows) --
        if progress_callback:
            progress_callback(0.05, "Fetching Neo4j CALLS relationships...")

        neo4j_set, neo4j_raw_count, snippet_count = await self._fetch_neo4j_edges(
            project_id, run_id,
        )

        if progress_callback:
            progress_callback(0.40, "Fetching codebase edges...")

        # -- Step 2: fetch codebase edges --
        raw_edges = self._index.get_edges()
        logger.info("Codebase raw edges: %d", len(raw_edges))
        if raw_edges:
            logger.info("Sample codebase edge: from=%s, to=%s", raw_edges[0]["from"], raw_edges[0]["to"])

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

        logger.info("Codebase unique edges: %d", len(codebase_set))
        if codebase_set:
            sample_key = next(iter(codebase_set))
            logger.info("Sample codebase edge key: %s", sample_key)

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
            neo4j_total_edges=neo4j_raw_count,
            codebase_total_edges=len(raw_edges),
            neo4j_snippet_count=snippet_count,
            codebase_function_count=codebase_func_count,
            confirmed=confirmed,
            missing_from_neo4j=missing_from_neo4j,
            phantom_in_neo4j=phantom_in_neo4j,
            coverage_score=coverage,
            metadata={
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

    async def _fetch_neo4j_edges(
        self, project_id: int, run_id: int,
    ) -> tuple[dict[_EdgeKey, AuditEdge], int, int]:
        """
        Directly query ALL Snippet-[:CALLS]->Snippet relationships
        for the given project/run — no execution flow dependency.

        Returns (edge_dict, raw_edge_count, snippet_count).
        """
        cypher = """
        MATCH (caller:Snippet)-[c:CALLS]->(callee:Snippet)
        WHERE caller.project_id = $pid AND caller.run_id = $rid
        RETURN caller.function_name AS caller_func,
               caller.name          AS caller_name,
               caller.file_path     AS caller_file,
               caller.file_name     AS caller_file_name,
               caller.class_name    AS caller_class,
               callee.function_name AS callee_func,
               callee.name          AS callee_name,
               callee.file_path     AS callee_file,
               callee.callee_file_name     AS callee_file_name,
               callee.class_name    AS callee_class
        """
        rows = await self._neo4j.query(cypher, {"pid": project_id, "rid": run_id})
        logger.info("Neo4j CALLS query returned %d rows", len(rows))
        if rows:
            logger.info("Sample row keys: %s", list(rows[0].keys()))
            logger.info("Sample row: %s", dict(rows[0]))

        snippet_cypher = """
        MATCH (s:Snippet {project_id: $pid, run_id: $rid})
        RETURN count(s) AS cnt
        """
        snippet_rows = await self._neo4j.query(
            snippet_cypher, {"pid": project_id, "rid": run_id},
        )
        snippet_count = snippet_rows[0]["cnt"] if snippet_rows else 0

        neo4j_set: dict[_EdgeKey, AuditEdge] = {}
        skipped = 0
        for row in rows:
            caller_func = row.get("caller_func") or row.get("caller_name") or ""
            callee_func = row.get("callee_func") or row.get("callee_name") or ""
            caller_file = row.get("caller_file") or row.get("caller_file_name") or ""
            callee_file = row.get("callee_file") or row.get("callee_file_name") or ""
            if not caller_func or not callee_func:
                skipped += 1
                if skipped <= 3:
                    logger.warning(
                        "Skipping row (empty func): caller_func=%r, callee_func=%r, row=%s",
                        caller_func, callee_func, dict(row),
                    )
                continue
            key = _normalise_key(caller_func, caller_file, callee_func, callee_file)
            if key not in neo4j_set:
                neo4j_set[key] = AuditEdge(
                    caller=caller_func,
                    callee=callee_func,
                    caller_file=caller_file,
                    callee_file=callee_file,
                    caller_class=row.get("caller_class") or "",
                    callee_class=row.get("callee_class") or "",
                    confidence=1.0,
                )

        logger.info(
            "Neo4j: %d raw rows, %d skipped (no func), %d unique edges, %d snippets",
            len(rows), skipped, len(neo4j_set), snippet_count,
        )
        if neo4j_set:
            sample_key = next(iter(neo4j_set))
            logger.info("Sample neo4j edge key: %s", sample_key)
        return neo4j_set, len(rows), snippet_count

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
