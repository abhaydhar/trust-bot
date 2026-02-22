"""
LangChain-based Orchestrator — coordinates the full agentic validation pipeline.

This is the top-level entry point that replaces the linear ValidationPipeline
with an LLM-coordinated pipeline where each step is handled by an autonomous
agent that can reason, use tools, and fall back gracefully.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from trustbot.agents.llm.analysis_agent import LLMAnalysisAgent
from trustbot.agents.llm.codebase_agent import LLMCodebaseAgent
from trustbot.agents.llm.neo4j_agent import LLMNeo4jAgent
from trustbot.agents.llm.report_agent import LLMReportAgent
from trustbot.agents.llm.verification_agent import LLMVerificationAgent
from trustbot.agents.normalization import NormalizationAgent
from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import CallGraphOutput, VerificationResult
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.llm.orchestrator")


def _create_llm():
    """
    Create a LangChain-compatible LLM using LiteLLM as the backend.

    Uses the ChatLiteLLM wrapper from langchain-community so that all LLM
    provider configuration (model, API keys, base URL) flows through the
    existing LiteLLM/env settings.
    """
    from trustbot.config import settings

    try:
        from langchain_litellm import ChatLiteLLM
    except ImportError:
        from langchain_community.chat_models import ChatLiteLLM

    kwargs: dict[str, Any] = {
        "model": settings.litellm_model,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }
    if settings.litellm_api_base:
        kwargs["api_base"] = settings.litellm_api_base
    if settings.litellm_api_key:
        kwargs["api_key"] = settings.litellm_api_key

    return ChatLiteLLM(**kwargs)


class AgenticPipeline:
    """
    Full LLM-driven validation pipeline using LangChain agents.

    Each step is handled by a specialized agent:
    1. LLMNeo4jAgent — fetches and interprets the Neo4j call graph
    2. LLMCodebaseAgent — builds an independent call graph from the index
    3. NormalizationAgent — normalizes both graphs (rule-based, fast)
    4. LLMVerificationAgent — compares graphs using LLM reasoning
    5. LLMAnalysisAgent — explains discrepancies
    6. LLMReportAgent — generates the final report

    Falls back to rule-based agents on LLM failure for reliability.
    """

    def __init__(
        self,
        neo4j_tool: Neo4jTool,
        code_index: CodeIndex | None = None,
        filesystem_tool=None,
        llm: Any | None = None,
    ) -> None:
        self._neo4j_tool = neo4j_tool
        self._code_index = code_index
        self._filesystem_tool = filesystem_tool
        self._llm = llm or _create_llm()

        self._neo4j_agent = LLMNeo4jAgent(neo4j_tool, self._llm)
        self._normalizer = NormalizationAgent()
        self._verification_agent = LLMVerificationAgent(
            self._llm, code_index, filesystem_tool,
        )
        self._analysis_agent = LLMAnalysisAgent(
            self._llm, code_index, filesystem_tool,
        )
        self._report_agent = LLMReportAgent(self._llm)

        # Codebase agent is created per-request since it needs the current index
        self._codebase_agent: LLMCodebaseAgent | None = None
        if code_index:
            self._codebase_agent = LLMCodebaseAgent(
                code_index, self._llm, filesystem_tool,
            )

    def set_code_index(self, code_index: CodeIndex) -> None:
        """Update the code index (e.g., after user indexes a new repo)."""
        self._code_index = code_index
        self._codebase_agent = LLMCodebaseAgent(
            code_index, self._llm, self._filesystem_tool,
        )
        self._verification_agent = LLMVerificationAgent(
            self._llm, code_index, self._filesystem_tool,
        )
        self._analysis_agent = LLMAnalysisAgent(
            self._llm, code_index, self._filesystem_tool,
        )

    @property
    def has_index(self) -> bool:
        return self._code_index is not None

    async def validate_flow(
        self,
        execution_flow_key: str,
        progress_callback: Callable | None = None,
    ) -> tuple[VerificationResult, str, CallGraphOutput, CallGraphOutput]:
        """
        Run the full agentic validation pipeline for one execution flow.

        Returns:
            (VerificationResult, markdown_report, neo4j_graph, index_graph)
        """
        # --- Step 1: Neo4j Agent fetches the call graph ---
        if progress_callback:
            progress_callback("agent1", "LLM Agent: Fetching call graph from Neo4j...")

        neo4j_graph, neo4j_observations = await self._neo4j_agent.fetch(
            execution_flow_key,
        )
        root_function = neo4j_graph.root_function
        root_file = neo4j_graph.metadata.get("root_file_path", "")
        root_class = neo4j_graph.metadata.get("root_class_name", "")

        logger.info(
            "Step 1 (Neo4j Agent): %d edges, root=%s (%s) [agent=%s]",
            len(neo4j_graph.edges),
            root_function,
            root_file,
            neo4j_graph.metadata.get("agent_type", "llm"),
        )

        # --- Step 2: Codebase Agent builds independent call graph ---
        if progress_callback:
            progress_callback(
                "agent2",
                f"LLM Agent: Building call graph from index (root: {root_function})...",
            )

        neo4j_hint_files: set[str] = set()
        for edge in neo4j_graph.edges:
            if edge.caller_file:
                neo4j_hint_files.add(edge.caller_file)
            if edge.callee_file:
                neo4j_hint_files.add(edge.callee_file)
        if root_file:
            neo4j_hint_files.add(root_file)

        codebase_observations: dict = {}
        if self._codebase_agent:
            index_graph, codebase_observations = await self._codebase_agent.build(
                root_function=root_function,
                execution_flow_id=execution_flow_key,
                root_class=root_class,
                root_file=root_file,
                neo4j_hint_files=neo4j_hint_files,
            )
        else:
            index_graph = CallGraphOutput(
                execution_flow_id=execution_flow_key,
                source="filesystem",
                root_function=root_function,
                edges=[],
                unresolved_callees=[],
                metadata={"error": "No codebase indexed. Use Code Indexer first."},
            )
            logger.warning("Step 2 skipped: no code index available")

        logger.info(
            "Step 2 (Codebase Agent): %d edges [agent=%s]",
            len(index_graph.edges),
            index_graph.metadata.get("agent_type", "llm"),
        )

        # --- Step 3: Normalize both graphs (rule-based — fast and reliable) ---
        if progress_callback:
            progress_callback("agent3", "Normalizing graphs for comparison...")

        neo4j_norm = self._normalizer.normalize(neo4j_graph)
        index_norm = self._normalizer.normalize(index_graph)

        # --- Step 4: Verification Agent compares the graphs ---
        if progress_callback:
            progress_callback("agent3", "LLM Agent: Comparing call graphs...")

        result = await self._verification_agent.verify(neo4j_norm, index_norm)

        logger.info(
            "Step 4 (Verification Agent): %d confirmed, %d phantom, %d missing "
            "(trust: %.0f%%) [agent=%s]",
            len(result.confirmed_edges),
            len(result.phantom_edges),
            len(result.missing_edges),
            result.flow_trust_score * 100,
            result.metadata.get("agent_type", "llm"),
        )

        # --- Step 5: Analysis Agent explains discrepancies ---
        analysis: dict = {}
        if result.phantom_edges or result.missing_edges:
            if progress_callback:
                progress_callback(
                    "agent3", "LLM Agent: Analyzing discrepancies..."
                )

            analysis = await self._analysis_agent.analyze(
                result, neo4j_graph, index_graph,
            )

            logger.info(
                "Step 5 (Analysis Agent): %d patterns, %d fixes [agent=%s]",
                len(analysis.get("systemic_patterns", analysis.get("likely_causes", []))),
                len(analysis.get("recommended_actions", analysis.get("fix_suggestions", []))),
                analysis.get("agent_type", "llm"),
            )

        # --- Step 6: Report Agent generates the report ---
        if progress_callback:
            progress_callback("agent3", "LLM Agent: Generating report...")

        report_md = await self._report_agent.generate_markdown(
            result,
            analysis=analysis,
            neo4j_observations=neo4j_observations,
            codebase_observations=codebase_observations,
        )

        logger.info(
            "Step 6 (Report Agent): %d chars report generated",
            len(report_md),
        )

        return result, report_md, neo4j_graph, index_graph

    async def validate_flows(
        self,
        flow_keys: list[str],
        max_concurrent: int | None = None,
        progress_callback: Callable | None = None,
    ) -> list[tuple[VerificationResult, str, CallGraphOutput, CallGraphOutput]]:
        """
        Validate multiple flows concurrently.

        Uses a semaphore to limit LLM concurrency and avoid rate-limiting.
        Falls back to sequential if concurrency=1.
        """
        from trustbot.config import settings

        concurrency = max_concurrent or settings.max_concurrent_llm_calls
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
