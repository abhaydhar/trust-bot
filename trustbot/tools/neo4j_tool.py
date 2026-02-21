from __future__ import annotations

import asyncio
import logging
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from trustbot.config import settings
from trustbot.models.graph import CallEdge, CallGraph, ExecutionFlow, ProjectCallGraph, Snippet
from trustbot.tools.base import BaseTool

logger = logging.getLogger("trustbot.tools.neo4j")

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5  # seconds, exponential


class Neo4jTool(BaseTool):
    """
    Tool for querying the Neo4j knowledge graph.

    Provides structured access to ExecutionFlow nodes, Snippet nodes,
    and the CALLS/INVOKES relationships that form the call graph.

    Enterprise best-practices applied:
    - Connection pool with configurable size and lifetime
    - TCP connect and acquisition timeouts
    - Automatic retry with exponential backoff on transient failures
    - keep_alive to detect dead connections early
    """

    name = "neo4j"
    description = (
        "Query the Neo4j knowledge graph to retrieve execution flows, "
        "code snippets, and call graph relationships."
    )

    def __init__(self) -> None:
        super().__init__()
        self._driver: AsyncDriver | None = None

    async def initialize(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_lifetime=settings.neo4j_max_connection_lifetime,
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
            connection_acquisition_timeout=settings.neo4j_connection_acquisition_timeout,
            connection_timeout=settings.neo4j_connection_timeout,
            max_transaction_retry_time=settings.neo4j_max_transaction_retry_time,
            keep_alive=settings.neo4j_keep_alive,
        )
        await self._driver.verify_connectivity()
        logger.info(
            "Connected to Neo4j at %s (pool_size=%d, conn_lifetime=%ds, "
            "acquisition_timeout=%ds, connect_timeout=%ds, keep_alive=%s)",
            settings.neo4j_uri,
            settings.neo4j_max_connection_pool_size,
            settings.neo4j_max_connection_lifetime,
            settings.neo4j_connection_acquisition_timeout,
            settings.neo4j_connection_timeout,
            settings.neo4j_keep_alive,
        )

    async def shutdown(self) -> None:
        if self._driver:
            await self._driver.close()
            self._driver = None

    @property
    def driver(self) -> AsyncDriver:
        if self._driver is None:
            raise RuntimeError("Neo4j driver not initialized. Call initialize() first.")
        return self._driver

    async def _run_with_retry(self, coro_factory, description: str = "query"):
        """
        Execute a coroutine factory with retry on transient Neo4j failures.
        
        coro_factory is a zero-arg callable that returns a fresh coroutine
        each time (necessary because a coroutine can only be awaited once).
        """
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Neo4j %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                        description, attempt, MAX_RETRIES, exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Neo4j %s failed after %d attempts: %s",
                        description, MAX_RETRIES, exc,
                    )
        raise last_error

    async def get_execution_flow(self, key: str) -> ExecutionFlow:
        """Fetch an ExecutionFlow node by its key, returning all properties."""
        query = """
        MATCH (ef:ExecutionFlow {key: $key})
        RETURN ef
        """
        async def _do():
            async with self.driver.session() as session:
                result = await session.run(query, key=key)
                record = await result.single()
            if record is None:
                raise ValueError(f"No ExecutionFlow found with key '{key}'")
            return self._node_to_execution_flow(record["ef"])
        
        return await self._run_with_retry(_do, f"get_execution_flow({key})")

    async def get_execution_flows_by_project(
        self, project_id: int, run_id: int
    ) -> list[ExecutionFlow]:
        """Fetch all ExecutionFlow nodes for a given project_id and run_id."""
        query = """
        MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})
        RETURN ef
        ORDER BY ef.name
        """
        async def _do():
            flows: list[ExecutionFlow] = []
            async with self.driver.session() as session:
                result = await session.run(query, pid=project_id, rid=run_id)
                async for record in result:
                    flows.append(self._node_to_execution_flow(record["ef"]))
            if not flows:
                raise ValueError(
                    f"No ExecutionFlows found for project_id={project_id}, run_id={run_id}"
                )
            logger.info(
                "Found %d ExecutionFlows for project_id=%d, run_id=%d",
                len(flows), project_id, run_id,
            )
            return flows
        
        return await self._run_with_retry(
            _do, f"get_execution_flows_by_project({project_id}, {run_id})"
        )

    async def get_project_call_graph(
        self, project_id: int, run_id: int
    ) -> ProjectCallGraph:
        """
        Build call graphs for ALL ExecutionFlows in a project run.
        Returns a ProjectCallGraph containing individual CallGraphs.
        """
        flows = await self.get_execution_flows_by_project(project_id, run_id)
        call_graphs: list[CallGraph] = []

        for ef in flows:
            cg = await self.get_call_graph(ef.key)
            call_graphs.append(cg)

        pcg = ProjectCallGraph(
            project_id=project_id,
            run_id=run_id,
            execution_flows=flows,
            call_graphs=call_graphs,
        )

        logger.info(
            "Project call graph: %d flows, %d unique snippets, %d total edges",
            len(flows), pcg.total_snippets, pcg.total_edges,
        )
        return pcg

    def _node_to_execution_flow(self, node) -> ExecutionFlow:
        """Convert a Neo4j ExecutionFlow node to our model."""
        return ExecutionFlow(
            key=node.get("key", ""),
            name=node.get("name", ""),
            description=node.get("description", ""),
            project_id=node.get("project_id"),
            run_id=node.get("run_id"),
            module_name=node.get("module_name", ""),
            flow_type=node.get("flow_type", ""),
            complexity=node.get("complexity", ""),
            properties=dict(node),
        )

    async def get_flow_participants(
        self, key: str, starts_flow_only: bool = True
    ) -> list[Snippet]:
        """
        Get Snippet nodes connected to an ExecutionFlow via PARTICIPATES_IN_FLOW.
        When starts_flow_only=True, only return snippets where the relationship
        has STARTS_FLOW=true (the entry points).
        """
        if starts_flow_only:
            query = """
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
            WHERE r.STARTS_FLOW = true
            RETURN s, r
            """
        else:
            query = """
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
            RETURN s, r
            """

        async def _do():
            snippets: list[Snippet] = []
            async with self.driver.session() as session:
                result = await session.run(query, key=key)
                async for record in result:
                    node = record["s"]
                    rel = record["r"]
                    snippets.append(self._node_to_snippet(
                        node, starts_flow=bool(rel.get("STARTS_FLOW", False))
                    ))
            return snippets
        
        return await self._run_with_retry(_do, f"get_flow_participants({key})")

    async def get_root_snippet(self, key: str) -> Snippet | None:
        """
        Find the ROOT Snippet for an ExecutionFlow — the first node with
        type='ROOT' and STARTS_FLOW=true. This is the entry point from which
        the call graph branches outward.
        """
        query = """
        MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
        WHERE s.type = 'ROOT' AND r.STARTS_FLOW = true
        RETURN s, r
        LIMIT 1
        """
        async def _do():
            async with self.driver.session() as session:
                result = await session.run(query, key=key)
                record = await result.single()
            if record is None:
                return None
            return self._node_to_snippet(
                record["s"], starts_flow=True
            )
        
        return await self._run_with_retry(_do, f"get_root_snippet({key})")

    def _node_to_snippet(self, node, starts_flow: bool = False) -> Snippet:
        """Convert a Neo4j Snippet node to our Snippet model."""
        return Snippet(
            id=node.get("key", str(node.element_id)),
            key=node.get("key", ""),
            function_name=node.get("function_name", ""),
            name=node.get("name", ""),
            class_name=node.get("class_name", ""),
            file_path=node.get("file_path", ""),
            file_name=node.get("file_name", ""),
            tech=node.get("tech", ""),
            line_start=node.get("start_line_number"),
            line_end=node.get("end_line_number"),
            snippet_code=node.get("snippet", ""),
            type=node.get("type", ""),
            module_name=node.get("module_name", ""),
            starts_flow=starts_flow,
            properties=dict(node),
        )

    async def get_call_graph(self, key: str) -> CallGraph:
        """
        Build the complete call graph for an ExecutionFlow.

        1. Fetches the ExecutionFlow node
        2. Finds all participating Snippets
        3. Traverses CALLS edges between Snippets
        4. Returns a CallGraph with all nodes and edges
        """
        ef = await self.get_execution_flow(key)

        all_participants = await self.get_flow_participants(key, starts_flow_only=False)
        entry_points = [s.id for s in all_participants if s.starts_flow]

        snippets: dict[str, Snippet] = {}
        for s in all_participants:
            snippets[s.id] = s

        query = """
        MATCH (ef:ExecutionFlow {key: $key})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
        OPTIONAL MATCH (s)-[c:CALLS]->(target:Snippet)
        RETURN s.key as caller_key, target.key as callee_key,
               properties(c) as call_props, properties(target) as target_props,
               target
        """

        async def _do():
            edges: list[CallEdge] = []
            seen_edges: set[tuple[str, str, int]] = set()
            async with self.driver.session() as session:
                result = await session.run(query, key=key)
                async for record in result:
                    caller_key = record["caller_key"]
                    callee_key = record["callee_key"]
                    if callee_key is None:
                        continue
                    call_props = dict(record["call_props"]) if record["call_props"] else {}
                    exec_order = call_props.get("execution_order", 0)
                    edge_key = (caller_key, callee_key, exec_order)
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    if callee_key not in snippets and record["target"] is not None:
                        snippets[callee_key] = self._node_to_snippet(record["target"])
                    edges.append(
                        CallEdge(
                            caller_id=caller_key,
                            callee_id=callee_key,
                            relationship_type="CALLS",
                            properties=call_props,
                        )
                    )
            return edges

        edges = await self._run_with_retry(_do, f"get_call_graph({key})")

        logger.info(
            "Call graph for '%s': %d snippets, %d edges, %d entry points",
            key, len(snippets), len(edges), len(entry_points),
        )

        return CallGraph(
            execution_flow=ef,
            snippets=snippets,
            edges=edges,
            entry_points=entry_points,
        )

    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        """
        Run an arbitrary Cypher query with guardrails.
        Only read operations are allowed (no CREATE, DELETE, SET, MERGE, REMOVE).
        """
        forbidden = {"CREATE", "DELETE", "SET", "MERGE", "REMOVE", "DETACH", "DROP"}
        tokens = cypher.upper().split()
        for token in tokens:
            if token in forbidden:
                raise PermissionError(
                    f"Write operation '{token}' is not allowed. This tool is read-only."
                )

        async def _do():
            results: list[dict] = []
            async with self.driver.session() as session:
                result = await session.run(cypher, params or {})
                async for record in result:
                    results.append(dict(record))
            return results
        
        return await self._run_with_retry(_do, "cypher_query")
