"""
LangChain-based Neo4j Agent — autonomously fetches and interprets call graphs.

Replaces the rule-based Agent1Neo4jFetcher with an LLM-driven agent that can
reason about graph structure, handle edge cases, and make decisions about
root function identification.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser

from trustbot.agents.llm.prompts import NEO4J_AGENT_SYSTEM
from trustbot.agents.llm.tools import build_neo4j_tools
from trustbot.models.agentic import (
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
)
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.llm.neo4j_agent")


class LLMNeo4jAgent:
    """
    LangChain-powered agent that fetches call graphs from Neo4j.

    Unlike the rule-based Agent1Neo4jFetcher, this agent uses LLM reasoning
    to interpret the graph structure, identify root functions even in ambiguous
    cases, and provide observations about the graph.
    """

    def __init__(self, neo4j_tool: Neo4jTool, llm: Any) -> None:
        self._neo4j_tool = neo4j_tool
        self._llm = llm
        self._tools = build_neo4j_tools(neo4j_tool)

    async def fetch(self, execution_flow_key: str) -> tuple[CallGraphOutput, dict]:
        """
        Fetch the call graph from Neo4j using LLM-driven reasoning.

        Returns:
            (CallGraphOutput, observations_dict)
        """
        llm_with_tools = self._llm.bind_tools(self._tools)

        messages = [
            SystemMessage(content=NEO4J_AGENT_SYSTEM),
            HumanMessage(content=(
                f"Fetch and analyze the call graph for execution flow: {execution_flow_key}\n\n"
                "Use the tools to:\n"
                "1. Get the root snippet\n"
                "2. Get the complete call graph\n"
                "3. Analyze the structure\n\n"
                "Return your analysis as the JSON format specified in your instructions."
            )),
        ]

        observations: dict = {}
        max_iterations = 10

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
                        from langchain_core.messages import ToolMessage
                        messages.append(ToolMessage(
                            content=result,
                            tool_call_id=tool_call["id"],
                        ))
                    else:
                        from langchain_core.messages import ToolMessage
                        messages.append(ToolMessage(
                            content=json.dumps({"error": f"Unknown tool: {tool_call['name']}"}),
                            tool_call_id=tool_call["id"],
                        ))
            else:
                # LLM returned a final response — parse it
                content = response.content
                try:
                    parsed = self._parse_json_response(content)
                    observations = parsed.get("observations", [])
                    output = self._build_output(execution_flow_key, parsed)
                    return output, {"observations": observations}
                except Exception as e:
                    logger.warning(
                        "Failed to parse LLM response, falling back to rule-based: %s",
                        str(e)[:200],
                    )
                    return await self._fallback_fetch(execution_flow_key), {}

        logger.warning("Neo4j agent hit max iterations, falling back to rule-based")
        return await self._fallback_fetch(execution_flow_key), {}

    def _parse_json_response(self, content: str) -> dict:
        """Extract JSON from the LLM response (may be wrapped in markdown)."""
        text = content.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1]
            text = text.split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1]
            text = text.split("```", 1)[0]
        return json.loads(text.strip())

    def _build_output(self, flow_key: str, parsed: dict) -> CallGraphOutput:
        """Convert parsed LLM JSON into CallGraphOutput."""
        edges = []
        for e in parsed.get("edges", []):
            edges.append(CallGraphEdge(
                caller=e.get("caller", ""),
                callee=e.get("callee", ""),
                caller_file=e.get("caller_file", ""),
                callee_file=e.get("callee_file", ""),
                caller_class=e.get("caller_class", ""),
                callee_class=e.get("callee_class", ""),
                depth=1,
                extraction_method=ExtractionMethod.NEO4J,
                confidence=1.0,
            ))

        return CallGraphOutput(
            execution_flow_id=flow_key,
            source=GraphSource.NEO4J,
            root_function=parsed.get("root_function", "unknown"),
            edges=edges,
            unresolved_callees=[],
            metadata={
                "root_file_path": parsed.get("root_file", ""),
                "root_class_name": parsed.get("root_class", ""),
                "total_nodes": parsed.get("total_nodes", len(edges)),
                "observations": parsed.get("observations", []),
                "agent_type": "llm",
            },
        )

    async def _fallback_fetch(self, execution_flow_key: str) -> CallGraphOutput:
        """Fall back to the rule-based Agent1 if LLM fails."""
        from trustbot.agents.agent1_neo4j import Agent1Neo4jFetcher

        logger.info("Using rule-based fallback for Neo4j fetch")
        agent = Agent1Neo4jFetcher(self._neo4j_tool)
        output = await agent.fetch(execution_flow_key)
        output.metadata["agent_type"] = "rule_based_fallback"
        return output
