"""
Multi-agent validation pipeline (3-agent architecture).

Supports two modes controlled by TRUSTBOT_AGENTIC_MODE:

  "llm"   — LangChain-based agents use LLM reasoning for every decision.
             Each agent autonomously fetches data, resolves ambiguities,
             and falls back to rules on LLM failure.

  "rules" — Original rule-based pipeline with hardcoded matching tiers,
             fixed trust score weights, and template-based reports.
             Faster, deterministic, zero LLM cost.

Agent 1 — Neo4j Graph Fetcher:
    Fetches the call graph from Neo4j for an execution flow.
    Identifies the ROOT Snippet (type='ROOT', STARTS_FLOW=true).

Agent 2 — Indexed Codebase Builder:
    Takes the ROOT function from Agent 1, traverses the indexed codebase
    (populated via the Code Indexer tab) to build an independent call graph.

Agent 3 — Comparison & Verification:
    Normalizes both graphs, diffs them, computes trust scores, and generates
    a report showing confirmed / phantom / missing edges.
"""

from __future__ import annotations

import asyncio
import logging

from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import CallGraphOutput, VerificationResult
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.pipeline")


def create_pipeline(
    neo4j_tool: Neo4jTool,
    code_index: CodeIndex | None = None,
    filesystem_tool=None,
    mode: str | None = None,
) -> "ValidationPipeline | AgenticPipeline":
    """
    Factory: create the appropriate pipeline based on config or explicit mode.

    Args:
        neo4j_tool: Initialized Neo4j tool instance.
        code_index: Optional CodeIndex (SQLite) for codebase lookups.
        filesystem_tool: Optional FilesystemTool for reading source files.
        mode: Override the config-level mode ("llm" or "rules").

    Returns:
        An AgenticPipeline (LLM-driven) or ValidationPipeline (rule-based).
    """
    effective_mode = (mode or settings.agentic_mode).lower().strip()

    if effective_mode == "llm":
        try:
            from trustbot.agents.llm.orchestrator import AgenticPipeline

            pipeline = AgenticPipeline(
                neo4j_tool=neo4j_tool,
                code_index=code_index,
                filesystem_tool=filesystem_tool,
            )
            logger.info("Created LLM-driven AgenticPipeline (LangChain)")
            return pipeline
        except Exception as e:
            logger.warning(
                "Failed to create AgenticPipeline, falling back to rules: %s",
                str(e)[:200],
            )
            effective_mode = "rules"

    logger.info("Created rule-based ValidationPipeline")
    return ValidationPipeline(
        neo4j_tool=neo4j_tool,
        code_index=code_index,
        filesystem_tool=filesystem_tool,
    )


class ValidationPipeline:
    """
    Rule-based per-flow validation pipeline (3 agents):

    1. Agent 1 fetches call graph + ROOT snippet from Neo4j
    2. Agent 2 builds call graph from indexed codebase starting at ROOT
    3. Agent 3 normalizes, diffs, scores, and reports
    """

    def __init__(
        self,
        neo4j_tool: Neo4jTool,
        code_index: CodeIndex | None = None,
        filesystem_tool=None,
    ) -> None:
        from trustbot.agents.agent1_neo4j import Agent1Neo4jFetcher
        from trustbot.agents.normalization import NormalizationAgent
        from trustbot.agents.report import ReportAgent
        from trustbot.agents.verification import VerificationAgent

        self._neo4j_tool = neo4j_tool
        self._agent1 = Agent1Neo4jFetcher(neo4j_tool)
        self._code_index = code_index
        self._normalizer = NormalizationAgent()
        self._verifier = VerificationAgent()
        self._reporter = ReportAgent()

    def set_code_index(self, code_index: CodeIndex) -> None:
        """Update the code index (e.g. after user indexes a new repo)."""
        self._code_index = code_index

    @property
    def has_index(self) -> bool:
        return self._code_index is not None

    async def validate_flow(
        self,
        execution_flow_key: str,
        progress_callback=None,
    ) -> tuple[VerificationResult, str, CallGraphOutput, CallGraphOutput]:
        """
        Run full 3-agent validation for one execution flow.

        Returns:
            (VerificationResult, markdown_report, neo4j_graph, index_graph)
        """
        from trustbot.agents.agent2_index import Agent2IndexBuilder

        # --- Agent 1: Fetch from Neo4j ---
        if progress_callback:
            progress_callback("agent1", "Fetching call graph from Neo4j...")
        neo4j_graph = await self._agent1.fetch(execution_flow_key)
        root_function = neo4j_graph.root_function
        root_file = neo4j_graph.metadata.get("root_file_path", "")

        logger.info(
            "Agent 1 complete: %d edges, root=%s (%s)",
            len(neo4j_graph.edges), root_function, root_file,
        )

        # --- Agent 2: Build from indexed codebase ---
        root_class = neo4j_graph.metadata.get("root_class_name", "")

        neo4j_hint_files: set[str] = set()
        for edge in neo4j_graph.edges:
            if edge.caller_file:
                neo4j_hint_files.add(edge.caller_file)
            if edge.callee_file:
                neo4j_hint_files.add(edge.callee_file)
        if root_file:
            neo4j_hint_files.add(root_file)

        if progress_callback:
            progress_callback("agent2", f"Building call graph from index (root: {root_function})...")

        if self._code_index is None:
            index_graph = CallGraphOutput(
                execution_flow_id=execution_flow_key,
                source="filesystem",
                root_function=root_function,
                edges=[],
                unresolved_callees=[],
                metadata={"error": "No codebase indexed. Use Code Indexer first."},
            )
            logger.warning("Agent 2 skipped: no code index available")
        else:
            agent2 = Agent2IndexBuilder(self._code_index)
            index_graph = await agent2.build(
                root_function=root_function,
                execution_flow_id=execution_flow_key,
                root_class=root_class,
                root_file=root_file,
                neo4j_hint_files=neo4j_hint_files,
            )

        logger.info(
            "Agent 2 complete: %d edges from index",
            len(index_graph.edges),
        )

        # --- Agent 3: Normalize, Verify, Report ---
        if progress_callback:
            progress_callback("agent3", "Comparing call graphs...")

        neo4j_norm = self._normalizer.normalize(neo4j_graph)
        index_norm = self._normalizer.normalize(index_graph)
        result = self._verifier.verify(neo4j_norm, index_norm)

        report_md = self._reporter.generate_markdown(result)

        logger.info(
            "Agent 3 complete: %d confirmed, %d phantom, %d missing (trust: %.0f%%)",
            len(result.confirmed_edges),
            len(result.phantom_edges),
            len(result.missing_edges),
            result.flow_trust_score * 100,
        )

        return result, report_md, neo4j_graph, index_graph

    async def validate_flows(
        self,
        flow_keys: list[str],
        max_concurrent: int | None = None,
        progress_callback=None,
    ) -> list[tuple[VerificationResult, str, CallGraphOutput, CallGraphOutput]]:
        """
        Validate multiple flows concurrently (rule-based pipeline).

        Rule-based agents are fast and mostly I/O-bound (Neo4j queries),
        so higher concurrency is safe.
        """
        concurrency = max_concurrent or min(settings.max_concurrent_llm_calls, 10)
        semaphore = asyncio.Semaphore(concurrency)

        async def _validate_one(idx: int, key: str):
            async with semaphore:
                def _flow_progress(agent, msg):
                    if progress_callback:
                        progress_callback(idx, len(flow_keys), agent, msg)
                try:
                    result = await self.validate_flow(key, progress_callback=_flow_progress)
                    if progress_callback:
                        progress_callback(idx, len(flow_keys), "done", "")
                    return result
                except Exception as e:
                    logger.exception("Flow %s failed: %s", key, e)
                    if progress_callback:
                        progress_callback(idx, len(flow_keys), "done", "")
                    empty_graph = CallGraphOutput(
                        execution_flow_id=key,
                        source="filesystem",
                        root_function="error",
                        edges=[],
                        unresolved_callees=[],
                        metadata={"error": str(e)},
                    )
                    error_result = VerificationResult(
                        execution_flow_id=key,
                        graph_trust_score=0.0,
                        flow_trust_score=0.0,
                        metadata={"error": str(e)},
                    )
                    return error_result, f"Error: {e}", empty_graph, empty_graph

        tasks = [_validate_one(i, key) for i, key in enumerate(flow_keys)]
        return await asyncio.gather(*tasks)

    async def validate(
        self,
        execution_flow_id: str,
        spec=None,
    ) -> tuple[VerificationResult, str]:
        """Legacy API — runs validate_flow and returns (result, report)."""
        result, report_md, _, _ = await self.validate_flow(execution_flow_id)
        return result, report_md
