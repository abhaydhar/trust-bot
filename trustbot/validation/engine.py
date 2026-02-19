"""
Validation engine — compares Neo4j call graph against the actual codebase.

Uses a layered approach:
  Layer 1: Targeted extraction (read only the relevant function, not the whole file)
  Layer 2: Edge-by-edge validation (one caller + its callees per LLM call)
  Layer 3: Pre-filter before LLM (cheap checks first — file exists, function exists)
  Layer 4: Map-Reduce aggregation (parallel LLM calls, deterministic merge)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import litellm

from trustbot.agent.prompts import VALIDATION_PROMPT
from trustbot.config import settings
from trustbot.models.graph import CallEdge, CallGraph, Snippet
from trustbot.models.validation import (
    EdgeStatus,
    EdgeVerdict,
    NodeStatus,
    NodeVerdict,
    ValidationReport,
)
from trustbot.tools.base import ToolRegistry

logger = logging.getLogger("trustbot.validation")


class ValidationEngine:
    """
    Validates a Neo4j call graph against the actual codebase.

    Strategy:
    1. Pre-filter: cheap filesystem checks (no LLM)
    2. LLM validation: batched per-caller, parallelized with semaphore
    3. Aggregation: merge results into a ValidationReport
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_llm_calls)

    async def validate(self, call_graph: CallGraph) -> ValidationReport:
        """Run full validation on a call graph."""
        report = ValidationReport(execution_flow_key=call_graph.execution_flow.key)

        # Phase 1: Validate all nodes (pre-filter, no LLM needed)
        logger.info("Phase 1: Validating %d nodes...", len(call_graph.snippets))
        node_tasks = [
            self._validate_node(snippet)
            for snippet in call_graph.snippets.values()
        ]
        node_results = await asyncio.gather(*node_tasks, return_exceptions=True)

        for result in node_results:
            if isinstance(result, Exception):
                logger.error("Node validation error: %s", result)
                continue
            report.node_results.append(result)

        # Build a lookup of valid/drifted nodes for edge validation
        valid_nodes = {
            n.snippet_id
            for n in report.node_results
            if n.verdict in (NodeVerdict.VALID, NodeVerdict.DRIFTED)
        }

        # Phase 2: Validate all edges (LLM-powered, batched per caller)
        logger.info("Phase 2: Validating %d edges...", len(call_graph.edges))
        edge_tasks = []
        for edge in call_graph.edges:
            if edge.caller_id not in valid_nodes:
                # Caller is missing — edge is automatically unconfirmed
                caller = call_graph.get_snippet(edge.caller_id)
                callee = call_graph.get_snippet(edge.callee_id)
                report.edge_results.append(
                    EdgeStatus(
                        caller_id=edge.caller_id,
                        callee_id=edge.callee_id,
                        caller_function=caller.function_name if caller else "",
                        callee_function=callee.function_name if callee else "",
                        verdict=EdgeVerdict.UNCONFIRMED,
                        confidence=0.0,
                        details="Caller function is missing from codebase",
                    )
                )
                continue

            edge_tasks.append(self._validate_edge(edge, call_graph))

        edge_results = await asyncio.gather(*edge_tasks, return_exceptions=True)

        for result in edge_results:
            if isinstance(result, Exception):
                logger.error("Edge validation error: %s", result)
                continue
            report.edge_results.append(result)

        report.compute_summary()
        logger.info(
            "Validation complete: %d/%d nodes valid, %d/%d edges confirmed",
            report.summary.valid_nodes,
            report.summary.total_nodes,
            report.summary.confirmed_edges,
            report.summary.total_edges,
        )
        return report

    async def _validate_node(self, snippet: Snippet) -> NodeStatus:
        """
        Validate a single Snippet node.

        Uses a layered strategy:
        1. Check if the snippet has embedded code in Neo4j (always valid by definition)
        2. Try filesystem validation if codebase is available locally
        3. Fall back to index search
        """
        fs = self._registry.get("filesystem")

        # If the snippet has embedded source code, it's structurally present in the KB
        has_embedded_code = bool(snippet.snippet_code and snippet.snippet_code.strip())

        # Try filesystem validation first
        try:
            file_exists = await fs.call("check_file_exists", path=snippet.file_path)
        except (PermissionError, Exception):
            file_exists = False

        if file_exists:
            func_exists = await fs.call(
                "check_function_exists",
                path=snippet.file_path,
                function_name=snippet.function_name,
            )

            if func_exists:
                # Verify line numbers if available
                if snippet.line_start and snippet.line_end:
                    try:
                        code = await fs.call(
                            "read_lines",
                            path=snippet.file_path,
                            start=snippet.line_start,
                            end=snippet.line_end,
                            buffer=0,
                        )
                        if snippet.function_name in code:
                            return NodeStatus(
                                snippet_id=snippet.id,
                                function_name=snippet.function_name,
                                file_path=snippet.file_path,
                                verdict=NodeVerdict.VALID,
                                confidence=0.95,
                                details="Function found at expected file and line range",
                            )
                    except Exception:
                        pass

                return NodeStatus(
                    snippet_id=snippet.id,
                    function_name=snippet.function_name,
                    file_path=snippet.file_path,
                    verdict=NodeVerdict.VALID,
                    confidence=0.85,
                    details="File and function name confirmed in filesystem",
                )

            # File exists but function not found — drifted
            if has_embedded_code:
                return NodeStatus(
                    snippet_id=snippet.id,
                    function_name=snippet.function_name,
                    file_path=snippet.file_path,
                    verdict=NodeVerdict.DRIFTED,
                    confidence=0.7,
                    details=(
                        f"File exists but function '{snippet.function_name}' not found. "
                        f"Code is available from Neo4j snippet."
                    ),
                )
            return NodeStatus(
                snippet_id=snippet.id,
                function_name=snippet.function_name,
                file_path=snippet.file_path,
                verdict=NodeVerdict.MISSING,
                confidence=0.85,
                details=f"File exists but function '{snippet.function_name}' not found",
            )

        # File not on local filesystem — check if we have embedded code
        if has_embedded_code:
            return NodeStatus(
                snippet_id=snippet.id,
                function_name=snippet.function_name,
                file_path=snippet.file_path,
                verdict=NodeVerdict.VALID,
                confidence=0.75,
                details=(
                    f"File not on local filesystem (path: {snippet.file_name}), "
                    f"but source code is available from Neo4j ({snippet.line_start}-{snippet.line_end})"
                ),
            )

        # Try index as last resort (if available)
        try:
            index = self._registry.get("index")
            search_results = await index.call(
                "search_function",
                function_name=snippet.function_name,
                class_name=snippet.class_name or None,
            )
            if search_results:
                best = search_results[0]
                return NodeStatus(
                    snippet_id=snippet.id,
                    function_name=snippet.function_name,
                    file_path=snippet.file_path,
                    verdict=NodeVerdict.DRIFTED,
                    confidence=0.6,
                    details=(
                        f"Original file not found, but function found in index at "
                        f"'{best['file_path']}' line {best['line_start']}"
                    ),
                )
        except (KeyError, Exception):
            pass

        return NodeStatus(
            snippet_id=snippet.id,
            function_name=snippet.function_name,
            file_path=snippet.file_path,
            verdict=NodeVerdict.MISSING,
            confidence=0.9,
            details=f"File not found and no embedded code available",
        )

    async def _get_function_code(self, snippet: Snippet) -> str | None:
        """
        Get the source code for a snippet using multiple strategies:
        1. Use the embedded snippet_code from Neo4j (fastest, always available)
        2. Fall back to filesystem extraction if needed
        """
        # Strategy 1: Use the snippet code embedded in Neo4j
        if snippet.snippet_code:
            return snippet.snippet_code

        # Strategy 2: Try extracting from the filesystem
        fs = self._registry.get("filesystem")
        try:
            code = await fs.call(
                "extract_function_body",
                path=snippet.file_path,
                function_name=snippet.function_name,
            )
            if code:
                return code
        except Exception:
            pass

        # Strategy 3: Try reading the file region by line numbers
        if snippet.line_start and snippet.line_end:
            try:
                return await fs.call(
                    "read_lines",
                    path=snippet.file_path,
                    start=snippet.line_start,
                    end=snippet.line_end,
                )
            except Exception:
                pass

        return None

    async def _validate_edge(self, edge: CallEdge, call_graph: CallGraph) -> EdgeStatus:
        """
        Validate a single call edge.

        1. Get the caller function's source code
        2. Quick text check: does the callee name appear?
        3. If ambiguous, ask the LLM to confirm
        """
        caller = call_graph.get_snippet(edge.caller_id)
        callee = call_graph.get_snippet(edge.callee_id)

        if not caller or not callee:
            return EdgeStatus(
                caller_id=edge.caller_id,
                callee_id=edge.callee_id,
                verdict=EdgeVerdict.UNCONFIRMED,
                confidence=0.0,
                details="Caller or callee snippet data missing from graph",
            )

        caller_code = await self._get_function_code(caller)

        if not caller_code:
            return EdgeStatus(
                caller_id=edge.caller_id,
                callee_id=edge.callee_id,
                caller_function=caller.function_name,
                callee_function=callee.function_name,
                verdict=EdgeVerdict.UNCONFIRMED,
                confidence=0.0,
                details=f"Could not retrieve caller function code for '{caller.function_name}'",
            )

        # Pre-filter: quick text check
        if callee.function_name not in caller_code:
            return EdgeStatus(
                caller_id=edge.caller_id,
                callee_id=edge.callee_id,
                caller_function=caller.function_name,
                callee_function=callee.function_name,
                verdict=EdgeVerdict.CONTRADICTED,
                confidence=0.85,
                details=(
                    f"Text search: '{callee.function_name}' does not appear "
                    f"in the body of '{caller.function_name}'"
                ),
            )

        # The callee name appears — use the LLM to confirm it's an actual call
        async with self._semaphore:
            return await self._llm_validate_edge(caller, callee, caller_code, edge)

    async def _llm_validate_edge(
        self,
        caller: Snippet,
        callee: Snippet,
        caller_code: str,
        edge: CallEdge,
    ) -> EdgeStatus:
        """Ask the LLM whether the caller actually calls the callee."""
        prompt = VALIDATION_PROMPT.format(
            caller_function=caller.function_name,
            caller_file=caller.file_path,
            caller_start=caller.line_start or "?",
            caller_end=caller.line_end or "?",
            callee_function=callee.function_name,
            callee_file=callee.file_path,
            caller_code=caller_code,
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise code analysis assistant. Respond only with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                **settings.get_litellm_kwargs(),
            )

            content = response.choices[0].message.content or "{}"
            result = json.loads(content)

            verdict_str = result.get("verdict", "UNCONFIRMED").upper()
            verdict = EdgeVerdict(verdict_str) if verdict_str in EdgeVerdict.__members__ else EdgeVerdict.UNCONFIRMED

            return EdgeStatus(
                caller_id=edge.caller_id,
                callee_id=edge.callee_id,
                caller_function=caller.function_name,
                callee_function=callee.function_name,
                verdict=verdict,
                confidence=float(result.get("confidence", 0.5)),
                details=result.get("details", ""),
            )

        except Exception as e:
            logger.error("LLM validation failed for edge %s->%s: %s", caller.function_name, callee.function_name, e)
            return EdgeStatus(
                caller_id=edge.caller_id,
                callee_id=edge.callee_id,
                caller_function=caller.function_name,
                callee_function=callee.function_name,
                verdict=EdgeVerdict.UNCONFIRMED,
                confidence=0.0,
                details=f"LLM validation error: {e}",
            )
