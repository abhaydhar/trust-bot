"""
LangChain-based Verification Agent — LLM-driven edge comparison and trust scoring.

Replaces the hardcoded 3-tier matching in verification.py with LLM reasoning
that can handle semantic name matching, ambiguous file paths, and contextual
confidence scoring.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from trustbot.agents.llm.prompts import (
    EDGE_VERIFICATION_PROMPT,
    VERIFICATION_AGENT_SYSTEM,
)
from trustbot.agents.llm.tools import build_verification_tools
from trustbot.models.agentic import (
    CallGraphOutput,
    EdgeClassification,
    ExtractionMethod,
    VerifiedEdge,
    VerificationResult,
    normalize_file_path,
)

logger = logging.getLogger("trustbot.agents.llm.verification_agent")


class LLMVerificationAgent:
    """
    LangChain-powered verification agent that compares call graphs using
    LLM reasoning instead of fixed matching tiers.

    Uses a two-phase approach:
    1. Bulk comparison: Send both graphs to the LLM for high-level matching
    2. Individual verification: For ambiguous edges, use tools to read source
       code and verify the call relationship
    """

    def __init__(
        self,
        llm: Any,
        code_index=None,
        filesystem_tool=None,
    ) -> None:
        self._llm = llm
        self._tools = build_verification_tools(code_index, filesystem_tool)
        self._code_index = code_index
        self._filesystem_tool = filesystem_tool

    async def verify(
        self,
        neo4j_graph: CallGraphOutput,
        fs_graph: CallGraphOutput,
    ) -> VerificationResult:
        """
        Compare two call graphs using LLM reasoning.

        Phase 1: Bulk comparison using the LLM to match edges intelligently.
        Phase 2: For low-confidence matches, use tools to verify.
        """
        neo4j_edges_data = self._edges_to_dicts(neo4j_graph)
        fs_edges_data = self._edges_to_dicts(fs_graph)

        # Phase 1: LLM-driven bulk comparison
        comparison_prompt = self._build_comparison_prompt(
            neo4j_graph.execution_flow_id,
            neo4j_edges_data,
            fs_edges_data,
        )

        llm_with_tools = self._llm.bind_tools(self._tools) if self._tools else self._llm

        messages = [
            SystemMessage(content=VERIFICATION_AGENT_SYSTEM),
            HumanMessage(content=comparison_prompt),
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
                            result = await tool._arun(**tool_call["args"])
                        except Exception as e:
                            result = json.dumps({"error": str(e)})
                        messages.append(ToolMessage(
                            content=result,
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
                    return self._build_result(neo4j_graph.execution_flow_id, parsed)
                except Exception as e:
                    logger.warning(
                        "Failed to parse verification response, falling back: %s",
                        str(e)[:200],
                    )
                    return self._fallback_verify(neo4j_graph, fs_graph)

        logger.warning("Verification agent hit max iterations, falling back")
        return self._fallback_verify(neo4j_graph, fs_graph)

    def _edges_to_dicts(self, graph: CallGraphOutput) -> list[dict]:
        """Convert graph edges to simple dicts for the LLM prompt."""
        result = []
        for e in graph.edges:
            result.append({
                "caller": e.caller,
                "callee": e.callee,
                "caller_file": normalize_file_path(e.caller_file),
                "callee_file": normalize_file_path(e.callee_file),
                "caller_class": e.caller_class,
                "callee_class": e.callee_class,
            })
        return result

    def _build_comparison_prompt(
        self,
        flow_id: str,
        neo4j_edges: list[dict],
        fs_edges: list[dict],
    ) -> str:
        """Build the comparison prompt for the LLM."""
        # Limit edges to avoid token overflow
        max_edges = 50
        neo4j_display = neo4j_edges[:max_edges]
        fs_display = fs_edges[:max_edges]
        truncated_neo4j = len(neo4j_edges) > max_edges
        truncated_fs = len(fs_edges) > max_edges

        parts = [
            f"Compare these two call graphs for execution flow: {flow_id}\n",
            f"## Neo4j Graph ({len(neo4j_edges)} edges):",
            json.dumps(neo4j_display, indent=2),
        ]
        if truncated_neo4j:
            parts.append(f"(... {len(neo4j_edges) - max_edges} more edges truncated)")

        parts.extend([
            f"\n## Codebase Graph ({len(fs_edges)} edges):",
            json.dumps(fs_display, indent=2),
        ])
        if truncated_fs:
            parts.append(f"(... {len(fs_edges) - max_edges} more edges truncated)")

        parts.extend([
            "\n## Instructions:",
            "Compare these graphs and classify every edge as CONFIRMED, PHANTOM, "
            "MISSING, or CONFLICTED.",
            "Use your tools to read source code if you need to verify ambiguous matches.",
            "Return the result as JSON per your system instructions.",
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

    def _build_result(self, flow_id: str, parsed: dict) -> VerificationResult:
        """Convert parsed LLM JSON into VerificationResult."""
        confirmed = []
        for e in parsed.get("confirmed_edges", []):
            confirmed.append(VerifiedEdge(
                caller=e.get("caller", ""),
                callee=e.get("callee", ""),
                caller_file=e.get("caller_file", ""),
                callee_file=e.get("callee_file", ""),
                classification=EdgeClassification.CONFIRMED,
                trust_score=e.get("trust_score", e.get("confidence", 0.85)),
                details=e.get("details", e.get("match_type", "LLM-verified")),
            ))

        phantom = []
        for e in parsed.get("phantom_edges", []):
            phantom.append(VerifiedEdge(
                caller=e.get("caller", ""),
                callee=e.get("callee", ""),
                caller_file=e.get("caller_file", ""),
                callee_file=e.get("callee_file", ""),
                classification=EdgeClassification.PHANTOM,
                trust_score=e.get("trust_score", 0.20),
                details=e.get("details", e.get("explanation", "LLM: Neo4j-only edge")),
            ))

        missing = []
        for e in parsed.get("missing_edges", []):
            missing.append(VerifiedEdge(
                caller=e.get("caller", ""),
                callee=e.get("callee", ""),
                caller_file=e.get("caller_file", ""),
                callee_file=e.get("callee_file", ""),
                classification=EdgeClassification.MISSING,
                trust_score=0.0,
                details=e.get("details", e.get("explanation", "LLM: codebase-only edge")),
            ))

        conflicted = []
        for e in parsed.get("conflicted_edges", []):
            conflicted.append(VerifiedEdge(
                caller=e.get("caller", ""),
                callee=e.get("callee", ""),
                caller_file=e.get("caller_file", ""),
                callee_file=e.get("callee_file", ""),
                classification=EdgeClassification.CONFLICTED,
                trust_score=e.get("trust_score", 0.30),
                details=e.get("details", e.get("explanation", "LLM: conflicting info")),
            ))

        graph_score = parsed.get("graph_trust_score", 0.0)
        flow_score = parsed.get("flow_trust_score", 0.0)

        # Compute scores if LLM didn't provide them
        if not graph_score and confirmed:
            total_score = sum(e.trust_score for e in confirmed)
            phantom_score = sum(e.trust_score * 0.5 for e in phantom)
            total_weight = len(confirmed) + len(phantom) * 0.5
            graph_score = (total_score + phantom_score) / total_weight if total_weight > 0 else 0.0

        if not flow_score:
            total_neo = len(confirmed) + len(phantom)
            flow_score = len(confirmed) / total_neo if total_neo > 0 else 0.0

        return VerificationResult(
            execution_flow_id=flow_id,
            graph_trust_score=graph_score,
            flow_trust_score=flow_score,
            confirmed_edges=confirmed,
            phantom_edges=phantom,
            missing_edges=missing,
            conflicted_edges=conflicted,
            unresolved_callees=[],
            metadata={
                "agent_type": "llm",
                "reasoning": parsed.get("reasoning", ""),
                "neo4j_edges": len(confirmed) + len(phantom),
                "filesystem_edges": len(confirmed) + len(missing),
                "confirmed": len(confirmed),
                "phantom": len(phantom),
                "missing": len(missing),
            },
        )

    def _fallback_verify(
        self,
        neo4j_graph: CallGraphOutput,
        fs_graph: CallGraphOutput,
    ) -> VerificationResult:
        """Fall back to the rule-based verification if LLM fails."""
        from trustbot.agents.verification import VerificationAgent

        logger.info("Using rule-based fallback for verification")
        agent = VerificationAgent()
        result = agent.verify(neo4j_graph, fs_graph)
        result.metadata["agent_type"] = "rule_based_fallback"
        return result
