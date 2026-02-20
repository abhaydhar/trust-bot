"""
Agent 1 — Neo4j Graph Fetcher.

Reconstructs the call graph purely from Neo4j. Has NO access to the filesystem
and NO knowledge of source code content. Emits output in shared format.
"""

from __future__ import annotations

import logging
from datetime import datetime

from trustbot.models.agentic import (
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
)
from trustbot.models.graph import CallGraph
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.agent1")


class Agent1Neo4jFetcher:
    """
    Agent that fetches the call graph from Neo4j only.
    All edges have confidence 1.0 and extraction_method=neo4j.
    """

    def __init__(self, neo4j_tool: Neo4jTool) -> None:
        self._neo4j = neo4j_tool

    async def fetch(self, execution_flow_id: str) -> CallGraphOutput:
        """
        Fetch the full call graph from Neo4j and emit in shared format.
        """
        call_graph: CallGraph = await self._neo4j.get_call_graph(execution_flow_id)

        root_function = ""
        if call_graph.entry_points:
            root_snippet = call_graph.get_snippet(call_graph.entry_points[0])
            if root_snippet:
                root_function = root_snippet.function_name or root_snippet.name or root_snippet.id

        edges: list[CallGraphEdge] = []
        for edge in call_graph.edges:
            caller = call_graph.get_snippet(edge.caller_id)
            callee = call_graph.get_snippet(edge.callee_id)
            caller_name = caller.function_name or caller.name or edge.caller_id if caller else edge.caller_id
            callee_name = callee.function_name or callee.name or edge.callee_id if callee else edge.callee_id
            caller_file = caller.file_path if caller else ""
            callee_file = callee.file_path if callee else ""

            edges.append(
                CallGraphEdge(
                    caller=caller_name,
                    callee=callee_name,
                    caller_file=caller_file,
                    callee_file=callee_file,
                    depth=1,
                    extraction_method=ExtractionMethod.NEO4J,
                    confidence=1.0,
                )
            )

        output = CallGraphOutput(
            execution_flow_id=execution_flow_id,
            source=GraphSource.NEO4J,
            root_function=root_function or "unknown",
            edges=edges,
            unresolved_callees=[],
            metadata={
                "total_depth": len(call_graph.snippets),
                "total_nodes": len(call_graph.snippets),
                "validation_timestamp": datetime.utcnow().isoformat() + "Z",
            },
        )

        logger.info(
            "Agent 1 fetched %d edges from Neo4j for flow %s",
            len(edges), execution_flow_id,
        )
        return output
