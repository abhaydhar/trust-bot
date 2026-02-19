"""
Agent orchestrator — coordinates the LLM, tools, and validation workflow.

Uses a direct function-calling approach with LiteLLM rather than a heavy
framework, keeping the control flow explicit and debuggable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import litellm

from trustbot.agent.prompts import SUMMARY_PROMPT, SYSTEM_PROMPT
from trustbot.config import settings
from trustbot.models.graph import CallGraph, ProjectCallGraph
from trustbot.models.validation import ProjectValidationReport, ValidationReport
from trustbot.tools.base import ToolRegistry
from trustbot.validation.engine import ValidationEngine

logger = logging.getLogger("trustbot.agent")


# LiteLLM tool definitions for function calling
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "neo4j__get_execution_flow",
            "description": "Fetch an ExecutionFlow node by its key from Neo4j",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The execution flow key"}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "neo4j__get_flow_participants",
            "description": "Get Snippet nodes linked to an ExecutionFlow. Set starts_flow_only=true to get only entry points.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The execution flow key"},
                    "starts_flow_only": {
                        "type": "boolean",
                        "description": "If true, only return snippets where STARTS_FLOW=true",
                        "default": True,
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "neo4j__get_call_graph",
            "description": "Build the complete call graph for an ExecutionFlow, including all snippets and edges",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The execution flow key"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filesystem__read_file",
            "description": "Read the full contents of a file from the codebase",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filesystem__read_lines",
            "description": "Read specific lines from a file (1-indexed, inclusive)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "start": {"type": "integer", "description": "Start line number"},
                    "end": {"type": "integer", "description": "End line number"},
                },
                "required": ["path", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filesystem__search_text",
            "description": "Search for text across all code files. Returns file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filesystem__extract_function_body",
            "description": "Extract the body of a named function from a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "function_name": {"type": "string", "description": "Name of the function"},
                },
                "required": ["path", "function_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "index__search_code",
            "description": "Semantic search over the indexed codebase",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Number of results", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "index__search_function",
            "description": "Find a function by name in the index",
            "parameters": {
                "type": "object",
                "properties": {
                    "function_name": {"type": "string", "description": "Function name"},
                    "class_name": {"type": "string", "description": "Optional class name"},
                    "file_path": {"type": "string", "description": "Optional file path"},
                },
                "required": ["function_name"],
            },
        },
    },
]


class AgentOrchestrator:
    """
    Coordinates the TrustBot agent workflow:
    1. Takes a user query (execution flow key)
    2. Uses LLM + tools to retrieve the call graph
    3. Runs the validation engine
    4. Generates a summary
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._validation_engine: ValidationEngine | None = None
        self._messages: list[dict] = []

    @property
    def validation_engine(self) -> ValidationEngine:
        if self._validation_engine is None:
            self._validation_engine = ValidationEngine(self._registry)
        return self._validation_engine

    async def process_key(self, key: str) -> tuple[ValidationReport, str]:
        """
        Validate a single call graph by its execution flow key.

        Returns:
            A tuple of (ValidationReport, conversational_summary).
        """
        logger.info("Processing execution flow key: %s", key)

        neo4j_tool = self._registry.get("neo4j")
        call_graph: CallGraph = await neo4j_tool.call("get_call_graph", key=key)

        logger.info(
            "Retrieved call graph: %d snippets, %d edges, %d entry points",
            len(call_graph.snippets),
            len(call_graph.edges),
            len(call_graph.entry_points),
        )

        report = await self.validation_engine.validate(call_graph)
        report.execution_flow_name = call_graph.execution_flow.name

        summary = await self._generate_summary(call_graph, report)
        report.llm_summary = summary

        return report, summary

    async def process_project(
        self, project_id: int, run_id: int
    ) -> tuple[ProjectValidationReport, str]:
        """
        Validate ALL execution flows for a project_id + run_id.

        Returns:
            A tuple of (ProjectValidationReport, conversational_summary).
        """
        logger.info("Processing project_id=%d, run_id=%d", project_id, run_id)

        neo4j_tool = self._registry.get("neo4j")
        project_graph: ProjectCallGraph = await neo4j_tool.call(
            "get_project_call_graph", project_id=project_id, run_id=run_id
        )

        logger.info(
            "Retrieved %d execution flows, %d snippets, %d edges",
            len(project_graph.execution_flows),
            project_graph.total_snippets,
            project_graph.total_edges,
        )

        project_report = ProjectValidationReport(
            project_id=project_id, run_id=run_id
        )

        for call_graph in project_graph.call_graphs:
            logger.info(
                "Validating flow: %s (%d snippets, %d edges)",
                call_graph.execution_flow.name,
                len(call_graph.snippets),
                len(call_graph.edges),
            )
            report = await self.validation_engine.validate(call_graph)
            report.execution_flow_name = call_graph.execution_flow.name
            project_report.flow_reports.append(report)

        project_report.compute_overall_summary()

        summary = await self._generate_project_summary(project_graph, project_report)
        project_report.llm_summary = summary

        return project_report, summary

    async def chat(self, user_message: str) -> str:
        """
        Free-form chat with the agent. The LLM can use tools to answer
        questions about execution flows, code, etc.
        """
        if not self._messages:
            self._messages.append({"role": "system", "content": SYSTEM_PROMPT})

        self._messages.append({"role": "user", "content": user_message})

        max_iterations = 15
        for _ in range(max_iterations):
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=self._messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                **settings.get_litellm_kwargs(),
            )

            choice = response.choices[0]
            message = choice.message

            self._messages.append(message.model_dump(exclude_none=True))

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    result = await self._execute_tool_call(tool_call)
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, default=str),
                    })
            else:
                return message.content or ""

        return "I was unable to complete the request within the allowed number of tool calls."

    async def _execute_tool_call(self, tool_call: Any) -> Any:
        """Route a tool call to the appropriate tool method."""
        func_name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        # Function names are formatted as "tool__method"
        parts = func_name.split("__", 1)
        if len(parts) != 2:
            return {"error": f"Invalid tool function name: {func_name}"}

        tool_name, method_name = parts

        try:
            tool = self._registry.get(tool_name)
            result = await tool.call(method_name, **args)

            if hasattr(result, "model_dump"):
                return result.model_dump()
            return result
        except Exception as e:
            logger.exception("Tool call %s failed", func_name)
            return {"error": str(e)}

    async def _generate_summary(
        self, call_graph: CallGraph, report: ValidationReport
    ) -> str:
        """Generate a conversational summary of the validation results."""
        report.compute_summary()
        s = report.summary

        node_lines = []
        for n in report.node_results:
            node_lines.append(
                f"  - {n.function_name} ({n.file_path}): {n.verdict.value} "
                f"(confidence: {n.confidence:.0%}) — {n.details}"
            )

        edge_lines = []
        for e in report.edge_results:
            edge_lines.append(
                f"  - {e.caller_function} -> {e.callee_function}: {e.verdict.value} "
                f"(confidence: {e.confidence:.0%}) — {e.details}"
            )

        prompt = SUMMARY_PROMPT.format(
            flow_key=call_graph.execution_flow.key,
            flow_name=call_graph.execution_flow.name,
            node_results="\n".join(node_lines) or "  (none)",
            edge_results="\n".join(edge_lines) or "  (none)",
            total_nodes=s.total_nodes,
            valid=s.valid_nodes,
            drifted=s.drifted_nodes,
            missing=s.missing_nodes,
            total_edges=s.total_edges,
            confirmed=s.confirmed_edges,
            unconfirmed=s.unconfirmed_edges,
            contradicted=s.contradicted_edges,
        )

        response = await litellm.acompletion(
            model=settings.litellm_model,
            messages=[
                {"role": "system", "content": "You are TrustBot, a code analysis assistant."},
                {"role": "user", "content": prompt},
            ],
            **settings.get_litellm_kwargs(),
        )

        return response.choices[0].message.content or ""

    async def _generate_project_summary(
        self, project_graph: ProjectCallGraph, report: ProjectValidationReport
    ) -> str:
        """Generate a conversational summary for the full project validation."""
        s = report.overall_summary

        flow_lines = []
        for fr in report.flow_reports:
            fr.compute_summary()
            fs = fr.summary
            flow_lines.append(
                f"  - {fr.execution_flow_name} ({fr.execution_flow_key}): "
                f"{fs.valid_nodes}/{fs.total_nodes} nodes valid, "
                f"{fs.confirmed_edges}/{fs.total_edges} edges confirmed"
            )

        prompt = (
            f"You are TrustBot. Summarize this project-level validation.\n\n"
            f"**Project ID**: {project_graph.project_id}, **Run ID**: {project_graph.run_id}\n"
            f"**Execution Flows validated**: {len(report.flow_reports)}\n\n"
            f"**Per-flow results**:\n{chr(10).join(flow_lines)}\n\n"
            f"**Overall**:\n"
            f"- Nodes: {s.total_nodes} total ({s.valid_nodes} valid, "
            f"{s.drifted_nodes} drifted, {s.missing_nodes} missing)\n"
            f"- Edges: {s.total_edges} total ({s.confirmed_edges} confirmed, "
            f"{s.unconfirmed_edges} unconfirmed, {s.contradicted_edges} contradicted)\n\n"
            f"Provide a concise overall health assessment and highlight any flows "
            f"that need attention."
        )

        response = await litellm.acompletion(
            model=settings.litellm_model,
            messages=[
                {"role": "system", "content": "You are TrustBot, a code analysis assistant."},
                {"role": "user", "content": prompt},
            ],
            **settings.get_litellm_kwargs(),
        )

        return response.choices[0].message.content or ""
