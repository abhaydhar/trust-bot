"""
Agent 1 — Neo4j Graph Fetcher.

Reconstructs the call graph purely from Neo4j. Has NO access to the filesystem
and NO knowledge of source code content. Emits output in shared format.

Also identifies the ROOT Snippet (type='ROOT', STARTS_FLOW=true) — the entry
point from which the call graph branches outward.
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
from trustbot.models.graph import CallGraph, Snippet
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
        root_file = ""
        root_snippet: Snippet | None = None

        # Try to find the ROOT snippet (type='ROOT', STARTS_FLOW=true)
        root_snippet = await self._neo4j.get_root_snippet(execution_flow_id)
        if root_snippet:
            root_function = root_snippet.function_name or root_snippet.name or root_snippet.id
            root_file = root_snippet.file_path or ""
        elif call_graph.entry_points:
            ep = call_graph.get_snippet(call_graph.entry_points[0])
            if ep:
                root_function = ep.function_name or ep.name or ep.id
                root_file = ep.file_path or ""

        edges: list[CallGraphEdge] = []
        for edge in call_graph.edges:
            caller = call_graph.get_snippet(edge.caller_id)
            callee = call_graph.get_snippet(edge.callee_id)
            caller_name = caller.function_name or caller.name or edge.caller_id if caller else edge.caller_id
            callee_name = callee.function_name or callee.name or edge.callee_id if callee else edge.callee_id
            caller_file = caller.file_path if caller else ""
            callee_file = callee.file_path if callee else ""
            caller_class = caller.class_name if caller else ""
            callee_class = callee.class_name if callee else ""

            edges.append(
                CallGraphEdge(
                    caller=caller_name,
                    callee=callee_name,
                    caller_file=caller_file,
                    callee_file=callee_file,
                    caller_class=caller_class,
                    callee_class=callee_class,
                    depth=1,
                    extraction_method=ExtractionMethod.NEO4J,
                    confidence=1.0,
                )
            )

        # Collect all unique function names for diagnostics
        all_func_names = sorted({
            (s.function_name or s.name or s.id)
            for s in call_graph.snippets.values()
            if s.function_name or s.name
        })

        output = CallGraphOutput(
            execution_flow_id=execution_flow_id,
            source=GraphSource.NEO4J,
            root_function=root_function or "unknown",
            edges=edges,
            unresolved_callees=[],
            metadata={
                "total_nodes": len(call_graph.snippets),
                "root_snippet_key": root_snippet.key if root_snippet else "",
                "root_function_name": root_function,
                "root_class_name": root_snippet.class_name if root_snippet else "",
                "root_file_path": root_file,
                "root_type": root_snippet.type if root_snippet else "",
                "all_function_names": all_func_names[:20],
            },
        )

        logger.info(
            "Agent 1 fetched %d edges from Neo4j for flow %s (root: %s [%s])",
            len(edges), execution_flow_id, root_function, root_file,
        )
        return output
