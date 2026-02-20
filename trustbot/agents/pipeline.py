"""
Multi-agent validation pipeline.

Orchestrates Agent 1, Agent 2, Normalization, Verification, and Report agents.
Runs the full dual-derivation validation for a single execution flow.
"""

from __future__ import annotations

import logging

from trustbot.agents.agent1_neo4j import Agent1Neo4jFetcher
from trustbot.agents.agent2_filesystem import Agent2FilesystemBuilder
from trustbot.agents.normalization import NormalizationAgent
from trustbot.agents.report import ReportAgent
from trustbot.agents.verification import VerificationAgent
from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import SpecFlowDocument, VerificationResult
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.pipeline")


class ValidationPipeline:
    """
    Full per-flow validation pipeline:
    1. Agent 1 fetches from Neo4j
    2. Agent 2 builds from filesystem (needs SpecFlowDocument)
    3. Normalization normalizes both
    4. Verification diffs and scores
    5. Report generates output
    """

    def __init__(
        self,
        neo4j_tool: Neo4jTool,
        filesystem_tool: FilesystemTool,
        code_index: CodeIndex,
    ) -> None:
        self._agent1 = Agent1Neo4jFetcher(neo4j_tool)
        self._agent2 = Agent2FilesystemBuilder(filesystem_tool, code_index)
        self._normalizer = NormalizationAgent()
        self._verifier = VerificationAgent()
        self._reporter = ReportAgent()

    async def validate(
        self,
        execution_flow_id: str,
        spec: SpecFlowDocument | None = None,
    ) -> tuple[VerificationResult, str]:
        """
        Run full validation for one execution flow.

        If spec is None, we derive it from Neo4j (root from entry point).
        Returns (VerificationResult, markdown_report).
        """
        # Agent 1: fetch from Neo4j
        neo4j_graph = await self._agent1.fetch(execution_flow_id)

        # Build spec from Neo4j if not provided
        if spec is None:
            spec = SpecFlowDocument(
                root_function=neo4j_graph.root_function,
                root_file_path=self._get_root_file_from_neo4j(neo4j_graph),
                language="python",
                execution_flow_id=execution_flow_id,
            )

        # Agent 2: build from filesystem
        fs_graph = await self._agent2.build(spec)

        # Normalize both
        neo4j_norm = self._normalizer.normalize(neo4j_graph)
        fs_norm = self._normalizer.normalize(fs_graph)

        # Verify
        result = self._verifier.verify(neo4j_norm, fs_norm)

        # Report
        report_md = self._reporter.generate_markdown(result)
        summary = self._reporter.generate_summary(result)

        logger.info("Pipeline complete for %s: %s", execution_flow_id, summary)
        return result, report_md

    def _get_root_file_from_neo4j(self, graph) -> str:
        """Extract root file path from Neo4j graph. Root is the caller of first edge."""
        root = graph.root_function.upper()
        for e in graph.edges:
            if e.caller.upper() == root:
                return e.caller_file or e.callee_file or ""
        if graph.edges:
            return graph.edges[0].caller_file or graph.edges[0].callee_file or ""
        return ""
