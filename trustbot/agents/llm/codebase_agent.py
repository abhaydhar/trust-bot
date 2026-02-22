"""
Hybrid Codebase Agent — fast rule-based traversal with LLM fallback for ambiguity.

Uses the rule-based Agent2IndexBuilder for the graph traversal (which is a pure
data lookup from SQLite — no LLM needed). Only invokes the LLM when the rule-based
agent hits an ambiguity it can't resolve (e.g. root function not found, zero
edges produced).

This is dramatically faster than the pure-LLM approach because graph traversal
involves dozens of sequential lookups that don't benefit from reasoning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from trustbot.agents.agent2_index import Agent2IndexBuilder
from trustbot.agents.llm.prompts import CODEBASE_AGENT_SYSTEM
from trustbot.agents.llm.tools import build_codebase_tools
from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import (
    CallGraphOutput,
    GraphSource,
)

logger = logging.getLogger("trustbot.agents.llm.codebase_agent")


class LLMCodebaseAgent:
    """
    Hybrid agent: fast rule-based traversal first, LLM only when stuck.

    Phase 1 (fast): Run the rule-based Agent2IndexBuilder. This does a BFS
    traversal of the SQLite index — pure data lookups, no LLM calls, sub-second.

    Phase 2 (LLM, only if needed): If phase 1 produces zero edges or can't
    find the root, invoke the LLM with tools to reason about resolution.
    """

    def __init__(
        self,
        code_index: CodeIndex,
        llm: Any,
        filesystem_tool=None,
    ) -> None:
        self._code_index = code_index
        self._llm = llm
        self._filesystem_tool = filesystem_tool
        self._tools = build_codebase_tools(code_index, filesystem_tool)

    async def build(
        self,
        root_function: str,
        execution_flow_id: str = "",
        root_class: str = "",
        root_file: str = "",
        neo4j_hint_files: set[str] | None = None,
        project_prefix: str = "",
    ) -> tuple[CallGraphOutput, dict]:
        """
        Build a call graph using rule-based traversal, with LLM fallback.

        Returns:
            (CallGraphOutput, observations_dict)
        """
        # Phase 1: fast rule-based traversal
        rule_agent = Agent2IndexBuilder(self._code_index)
        output = await rule_agent.build(
            root_function=root_function,
            execution_flow_id=execution_flow_id,
            root_class=root_class,
            root_file=root_file,
            neo4j_hint_files=neo4j_hint_files,
        )

        root_found = output.metadata.get("root_found_in_index", False)
        root_has_edges = output.metadata.get("root_has_outgoing_edges", False)
        edge_count = len(output.edges)

        # If rule-based succeeded (found root + produced edges), return directly
        if root_found and edge_count > 0:
            output.metadata["agent_type"] = "rule_based"
            logger.info(
                "Phase 1 (rule-based) succeeded: %d edges for root=%s",
                edge_count, root_function,
            )
            return output, {"phase": "rule_based", "edges": edge_count}

        # Phase 2: LLM fallback for ambiguous cases
        logger.info(
            "Phase 1 produced %d edges (root_found=%s, root_has_edges=%s). "
            "Invoking LLM for resolution...",
            edge_count, root_found, root_has_edges,
        )

        llm_output = await self._llm_resolve(
            root_function=root_function,
            execution_flow_id=execution_flow_id,
            root_class=root_class,
            root_file=root_file,
            neo4j_hint_files=neo4j_hint_files,
            rule_based_meta=output.metadata,
        )

        if llm_output and len(llm_output.edges) > edge_count:
            llm_output.metadata["agent_type"] = "llm_resolved"
            return llm_output, {"phase": "llm_resolved"}

        # LLM didn't improve — return the rule-based result
        output.metadata["agent_type"] = "rule_based"
        return output, {"phase": "rule_based_final", "edges": edge_count}

    async def _llm_resolve(
        self,
        root_function: str,
        execution_flow_id: str,
        root_class: str,
        root_file: str,
        neo4j_hint_files: set[str] | None,
        rule_based_meta: dict,
    ) -> CallGraphOutput | None:
        """Use LLM to resolve ambiguities that the rule-based agent couldn't."""
        llm_with_tools = self._llm.bind_tools(self._tools)

        root_file_name = root_file.replace("\\", "/").rsplit("/", 1)[-1] if root_file else ""

        # Provide context about what went wrong in rule-based phase
        context_parts = [
            f"The rule-based codebase agent could not fully build the call graph for "
            f"execution flow: {execution_flow_id}",
            f"\nRoot function: `{root_function}`",
        ]
        if root_class:
            context_parts.append(f"Root class: `{root_class}`")
        if root_file_name:
            context_parts.append(f"Root filename (from Neo4j, reference only): `{root_file_name}`")

        # Tell LLM what the rule-based agent found
        sample_funcs = rule_based_meta.get("sample_index_functions", [])[:10]
        sample_edges = rule_based_meta.get("sample_edge_callers", [])[:10]
        context_parts.extend([
            f"\nRule-based agent found root in index: {rule_based_meta.get('root_found_in_index')}",
            f"Root has outgoing edges: {rule_based_meta.get('root_has_outgoing_edges')}",
            f"Resolved via: {rule_based_meta.get('resolved_via', 'unknown')}",
            f"Project prefix: {rule_based_meta.get('project_prefix', 'none')}",
            f"\nSample indexed functions: {sample_funcs}",
            f"Sample edge callers: {sample_edges}",
            "\nYour task: find the root function in the code index and build "
            "the call graph. Return JSON per your system instructions.",
        ])

        if neo4j_hint_files:
            hint_filenames = sorted({
                f.replace("\\", "/").rsplit("/", 1)[-1] for f in neo4j_hint_files
            })[:10]
            context_parts.append(f"Neo4j filenames (reference only): {hint_filenames}")

        messages = [
            SystemMessage(content=CODEBASE_AGENT_SYSTEM),
            HumanMessage(content="\n".join(context_parts)),
        ]

        max_iterations = 10

        for _ in range(max_iterations):
            try:
                response = await llm_with_tools.ainvoke(messages)
            except Exception as e:
                logger.warning("LLM call failed in codebase agent: %s", str(e)[:200])
                return None

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
                            content=json.dumps({"error": f"Unknown tool: {tool_call['name']}"}),
                            tool_call_id=tool_call["id"],
                        ))
            else:
                content = response.content
                try:
                    parsed = self._parse_json_response(content)
                    return self._build_output(execution_flow_id, root_function, parsed)
                except Exception as e:
                    logger.warning("Failed to parse LLM response: %s", str(e)[:200])
                    return None

        return None

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

    def _build_output(
        self,
        flow_key: str,
        original_root: str,
        parsed: dict,
    ) -> CallGraphOutput:
        """Convert parsed LLM JSON into CallGraphOutput."""
        from trustbot.models.agentic import CallGraphEdge, ExtractionMethod

        edges = []
        for e in parsed.get("edges", []):
            edges.append(CallGraphEdge(
                caller=e.get("caller", ""),
                callee=e.get("callee", ""),
                caller_file=e.get("caller_file", ""),
                callee_file=e.get("callee_file", ""),
                caller_class=e.get("caller_class", ""),
                callee_class=e.get("callee_class", ""),
                depth=e.get("depth", 1),
                extraction_method=ExtractionMethod.LLM_TIER2,
                confidence=e.get("confidence", 0.85),
            ))

        resolved_root = parsed.get("root_function", original_root)
        return CallGraphOutput(
            execution_flow_id=flow_key,
            source=GraphSource.FILESYSTEM,
            root_function=resolved_root,
            edges=edges,
            unresolved_callees=parsed.get("unresolved", []),
            metadata={
                "original_root": original_root,
                "resolved_root": resolved_root,
                "resolved_via": parsed.get("resolved_via", "llm_agent"),
                "root_file_hint": parsed.get("root_file", ""),
                "observations": parsed.get("observations", []),
                "agent_type": "llm_resolved",
            },
        )
