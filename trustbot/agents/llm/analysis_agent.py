"""
LangChain-based Analysis Agent — explains discrepancies and suggests fixes.

Replaces the template-based flow_attention.py with an LLM-driven agent that
can read actual source code, investigate phantom/missing edges, and provide
contextual explanations and actionable fixes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from trustbot.agents.llm.prompts import ANALYSIS_AGENT_SYSTEM
from trustbot.agents.llm.tools import build_verification_tools
from trustbot.models.agentic import CallGraphOutput, VerificationResult

logger = logging.getLogger("trustbot.agents.llm.analysis_agent")


class LLMAnalysisAgent:
    """
    LangChain-powered agent that analyzes verification discrepancies.

    Unlike the template-based flow_attention.py, this agent:
    - Actually reads source code to understand why edges are phantom/missing
    - Identifies systemic patterns across multiple edges
    - Provides contextual, specific fix recommendations
    """

    def __init__(
        self,
        llm: Any,
        code_index=None,
        filesystem_tool=None,
    ) -> None:
        self._llm = llm
        self._tools = build_verification_tools(code_index, filesystem_tool)

    async def analyze(
        self,
        result: VerificationResult,
        neo4j_graph: CallGraphOutput | None = None,
        index_graph: CallGraphOutput | None = None,
    ) -> dict:
        """
        Analyze verification discrepancies using LLM reasoning.

        Returns a structured analysis dict compatible with the existing
        flow_attention format.
        """
        if not result.phantom_edges and not result.missing_edges:
            return {
                "phantom_analysis": [],
                "missing_analysis": [],
                "root_analysis": {"message": "No discrepancies to analyze."},
                "systemic_patterns": [],
                "recommended_actions": [],
            }

        analysis_prompt = self._build_analysis_prompt(result, neo4j_graph, index_graph)

        llm_with_tools = self._llm.bind_tools(self._tools) if self._tools else self._llm
        messages = [
            SystemMessage(content=ANALYSIS_AGENT_SYSTEM),
            HumanMessage(content=analysis_prompt),
        ]

        max_iterations = 15

        for _ in range(max_iterations):
            response = await llm_with_tools.ainvoke(messages)
            messages.append(response)

            if response.tool_calls:
                for tool_call in response.tool_calls:
                    tool = next(
                        (t for t in self._tools if t.name == tool_call["name"]),
                        None,
                    )
                    if tool:
                        try:
                            tool_result = await tool._arun(**tool_call["args"])
                        except Exception as e:
                            tool_result = json.dumps({"error": str(e)})
                        messages.append(ToolMessage(
                            content=tool_result,
                            tool_call_id=tool_call["id"],
                        ))
                    else:
                        messages.append(ToolMessage(
                            content=json.dumps(
                                {"error": f"Unknown tool: {tool_call['name']}"}
                            ),
                            tool_call_id=tool_call["id"],
                        ))
            else:
                content = response.content
                try:
                    parsed = self._parse_json_response(content)
                    return self._normalize_analysis(parsed)
                except Exception as e:
                    logger.warning(
                        "Failed to parse analysis response, falling back: %s",
                        str(e)[:200],
                    )
                    return self._fallback_analyze(result, neo4j_graph, index_graph)

        logger.warning("Analysis agent hit max iterations, falling back")
        return self._fallback_analyze(result, neo4j_graph, index_graph)

    def _build_analysis_prompt(
        self,
        result: VerificationResult,
        neo4j_graph: CallGraphOutput | None,
        index_graph: CallGraphOutput | None,
    ) -> str:
        """Build the analysis prompt with discrepancy details."""
        parts = [
            f"Analyze discrepancies for execution flow: {result.execution_flow_id}\n",
            f"Trust scores: flow={result.flow_trust_score:.2%}, graph={result.graph_trust_score:.2%}",
            f"Confirmed: {len(result.confirmed_edges)}, "
            f"Phantom: {len(result.phantom_edges)}, "
            f"Missing: {len(result.missing_edges)}",
        ]

        # Root info from index graph
        if index_graph:
            meta = index_graph.metadata
            parts.extend([
                f"\nRoot function: {index_graph.root_function}",
                f"Root found in index: {meta.get('root_found_in_index', 'unknown')}",
                f"Resolved via: {meta.get('resolved_via', 'unknown')}",
                f"Project prefix: {meta.get('project_prefix', 'none')}",
            ])

        # Phantom edges (limit to avoid token overflow)
        # Strip file paths to filenames — edges may contain Neo4j remote paths
        def _fname(path: str) -> str:
            return path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""

        max_edges = 20
        if result.phantom_edges:
            parts.append(f"\n## Phantom Edges ({len(result.phantom_edges)} total):")
            for e in result.phantom_edges[:max_edges]:
                parts.append(
                    f"- `{e.caller}` → `{e.callee}` "
                    f"(files: {_fname(e.caller_file)}, {_fname(e.callee_file)})"
                )
            if len(result.phantom_edges) > max_edges:
                parts.append(f"(... {len(result.phantom_edges) - max_edges} more)")

        if result.missing_edges:
            parts.append(f"\n## Missing Edges ({len(result.missing_edges)} total):")
            for e in result.missing_edges[:max_edges]:
                parts.append(
                    f"- `{e.caller}` → `{e.callee}` "
                    f"(files: {_fname(e.caller_file)}, {_fname(e.callee_file)})"
                )
            if len(result.missing_edges) > max_edges:
                parts.append(f"(... {len(result.missing_edges) - max_edges} more)")

        parts.extend([
            "\nIMPORTANT: The file names above are for reference only. To read actual",
            "source code, first use code_index_search_function to find the LOCAL path",
            "in the index, then use that path with filesystem tools. Do NOT use Neo4j",
            "paths (like /mnt/storage/...) — they are remote server paths.",
            "",
            "Use your tools to investigate the phantom and missing edges.",
            "Search for the functions by NAME in the code index, read source code if helpful,",
            "and identify root causes and systemic patterns.",
            "Return your analysis as JSON per your system instructions.",
        ])

        return "\n".join(parts)

    def _parse_json_response(self, content: str) -> dict:
        """Extract JSON from the LLM response."""
        text = content.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1]
            text = text.split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1]
            text = text.split("```", 1)[0]
        return json.loads(text.strip())

    def _normalize_analysis(self, parsed: dict) -> dict:
        """Normalize the LLM output into the expected analysis format."""
        # Convert phantom_analysis to phantom_reasons format for backward compat
        phantom_reasons = []
        for item in parsed.get("phantom_analysis", []):
            phantom_reasons.append({
                "caller": item.get("caller", ""),
                "callee": item.get("callee", ""),
                "reason": item.get("root_cause", item.get("reason", "")),
                "fix_suggestion": item.get("fix_suggestion", item.get("fix", "")),
            })

        missing_reasons = []
        for item in parsed.get("missing_analysis", []):
            missing_reasons.append({
                "caller": item.get("caller", ""),
                "callee": item.get("callee", ""),
                "reason": item.get("root_cause", item.get("reason", "")),
                "fix_suggestion": item.get("fix_suggestion", item.get("fix", "")),
            })

        root_analysis = parsed.get("root_analysis", {})

        likely_causes = parsed.get("systemic_patterns", [])
        fix_suggestions = parsed.get("recommended_actions", [])

        return {
            "phantom_reasons": phantom_reasons,
            "missing_reasons": missing_reasons,
            "root_analysis": root_analysis,
            "likely_causes": likely_causes,
            "fix_suggestions": fix_suggestions,
            "agent_type": "llm",
        }

    def _fallback_analyze(
        self,
        result: VerificationResult,
        neo4j_graph: CallGraphOutput | None,
        index_graph: CallGraphOutput | None,
    ) -> dict:
        """Fall back to rule-based analysis if LLM fails."""
        from trustbot.agents.flow_attention import analyze_flow_attention

        logger.info("Using rule-based fallback for analysis")
        analysis = analyze_flow_attention(result, neo4j_graph, index_graph)
        analysis["agent_type"] = "rule_based_fallback"
        return analysis
