"""
LangChain-based Report Agent — generates intelligent validation reports.

Replaces the template-based report.py with an LLM-driven agent that
produces contextual, actionable reports with executive summaries.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from trustbot.agents.llm.prompts import REPORT_AGENT_SYSTEM
from trustbot.models.agentic import VerificationResult

logger = logging.getLogger("trustbot.agents.llm.report_agent")


class LLMReportAgent:
    """
    LangChain-powered report generator.

    Unlike the template-based ReportAgent, this agent uses the LLM to:
    - Write executive summaries that explain what the numbers mean
    - Highlight critical issues prominently
    - Provide prioritized, actionable recommendations
    - Adapt the report structure based on the severity of findings
    """

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def generate_markdown(
        self,
        result: VerificationResult,
        analysis: dict | None = None,
        neo4j_observations: dict | None = None,
        codebase_observations: dict | None = None,
    ) -> str:
        """
        Generate a comprehensive validation report using LLM reasoning.
        """
        report_prompt = self._build_report_prompt(
            result, analysis, neo4j_observations, codebase_observations,
        )

        messages = [
            SystemMessage(content=REPORT_AGENT_SYSTEM),
            HumanMessage(content=report_prompt),
        ]

        try:
            response = await self._llm.ainvoke(messages)
            content = response.content

            if not content or len(content.strip()) < 50:
                logger.warning("LLM report too short, falling back")
                return self._fallback_report(result)

            return content.strip()

        except Exception as e:
            logger.warning("Report generation failed, falling back: %s", str(e)[:200])
            return self._fallback_report(result)

    async def generate_summary(self, result: VerificationResult) -> str:
        """Generate a short one-line summary using the LLM."""
        prompt = (
            f"Write a single-sentence summary of this validation result:\n"
            f"- Flow: {result.execution_flow_id}\n"
            f"- Trust: {result.flow_trust_score:.0%}\n"
            f"- Confirmed: {len(result.confirmed_edges)}\n"
            f"- Phantom: {len(result.phantom_edges)}\n"
            f"- Missing: {len(result.missing_edges)}\n"
            f"Keep it under 100 words. Focus on the health status and any concerns."
        )

        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            return response.content.strip()
        except Exception:
            total = (
                len(result.confirmed_edges)
                + len(result.phantom_edges)
                + len(result.missing_edges)
            )
            if total == 0:
                return "No edges to validate."
            return (
                f"Flow {result.execution_flow_id}: "
                f"{result.flow_trust_score:.0%} trust — "
                f"{len(result.confirmed_edges)} confirmed, "
                f"{len(result.phantom_edges)} phantom, "
                f"{len(result.missing_edges)} missing"
            )

    def _build_report_prompt(
        self,
        result: VerificationResult,
        analysis: dict | None,
        neo4j_observations: dict | None,
        codebase_observations: dict | None,
    ) -> str:
        """Build the data payload for the report LLM."""
        parts = [
            f"Generate a validation report for execution flow: {result.execution_flow_id}\n",
            "## Verification Results",
            f"- Flow Trust Score: {result.flow_trust_score:.2%}",
            f"- Graph Trust Score: {result.graph_trust_score:.2%}",
            f"- Confirmed edges: {len(result.confirmed_edges)}",
            f"- Phantom edges (Neo4j only): {len(result.phantom_edges)}",
            f"- Missing edges (codebase only): {len(result.missing_edges)}",
            f"- Conflicted edges: {len(result.conflicted_edges)}",
            f"- Unresolved callees: {len(result.unresolved_callees)}",
        ]

        # Match tier breakdown if available
        meta = result.metadata
        if meta.get("match_full") is not None:
            parts.extend([
                f"\nMatch breakdown:",
                f"  - Full match (name+class+file): {meta.get('match_full', 0)}",
                f"  - Name+file match: {meta.get('match_name_file', 0)}",
                f"  - Name-only match: {meta.get('match_name_only', 0)}",
            ])

        # Confirmed edges (sample)
        if result.confirmed_edges:
            parts.append(f"\n## Confirmed Edges (showing up to 30):")
            for e in result.confirmed_edges[:30]:
                parts.append(
                    f"- `{e.caller}` → `{e.callee}` "
                    f"(score: {e.trust_score:.2f}, match: {e.details})"
                )

        # Phantom edges
        if result.phantom_edges:
            parts.append(f"\n## Phantom Edges:")
            for e in result.phantom_edges[:20]:
                parts.append(f"- `{e.caller}` → `{e.callee}` (score: {e.trust_score:.2f})")

        # Missing edges
        if result.missing_edges:
            parts.append(f"\n## Missing Edges:")
            for e in result.missing_edges[:20]:
                parts.append(f"- `{e.caller}` → `{e.callee}`")

        # Execution order
        order_mismatches = meta.get("execution_order_mismatches", [])
        if order_mismatches:
            parts.append(f"\n## Execution Order Mismatches: {len(order_mismatches)}")
            for m in order_mismatches[:5]:
                parts.append(
                    f"- `{m['caller']}`: Neo4j={m['neo4j_order']} vs Index={m['index_order']}"
                )

        # Analysis results
        if analysis:
            patterns = analysis.get("systemic_patterns") or analysis.get("likely_causes", [])
            if patterns:
                parts.append(f"\n## Identified Patterns:")
                for p in patterns:
                    parts.append(f"- {p}")

            fixes = analysis.get("recommended_actions") or analysis.get("fix_suggestions", [])
            if fixes:
                parts.append(f"\n## Suggested Fixes:")
                for f in fixes:
                    parts.append(f"- {f}")

        # Agent observations
        if neo4j_observations and neo4j_observations.get("observations"):
            parts.append(f"\n## Neo4j Agent Observations:")
            for obs in neo4j_observations["observations"]:
                parts.append(f"- {obs}")

        if codebase_observations and codebase_observations.get("observations"):
            parts.append(f"\n## Codebase Agent Observations:")
            for obs in codebase_observations["observations"]:
                parts.append(f"- {obs}")

        parts.append(
            "\nGenerate the full Markdown report following the structure "
            "in your system instructions."
        )

        return "\n".join(parts)

    def _fallback_report(self, result: VerificationResult) -> str:
        """Fall back to the rule-based report if LLM fails."""
        from trustbot.agents.report import ReportAgent

        logger.info("Using rule-based fallback for report generation")
        return ReportAgent().generate_markdown(result)
