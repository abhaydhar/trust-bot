"""
LangChain tool wrappers for TrustBot's existing tool layer.

Wraps Neo4jTool, CodeIndex, and FilesystemTool as LangChain-compatible tools
so that LangChain agents can invoke them autonomously.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from langchain_core.tools import BaseTool as LCBaseTool
from pydantic import BaseModel, Field

from trustbot.index.code_index import CodeIndex
from trustbot.tools.neo4j_tool import Neo4jTool

logger = logging.getLogger("trustbot.agents.llm.tools")


def _run_async(coro):
    """Run an async coroutine from a sync context, handling event loop reuse."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Neo4j tools
# ---------------------------------------------------------------------------


class GetCallGraphInput(BaseModel):
    execution_flow_key: str = Field(description="The execution flow key to fetch the call graph for")


class Neo4jGetCallGraphTool(LCBaseTool):
    """Fetch the complete call graph for an execution flow from Neo4j."""

    name: str = "neo4j_get_call_graph"
    description: str = (
        "Fetch the complete call graph (all snippets and call edges) for a given "
        "execution flow key from Neo4j. Returns nodes with function names, file paths, "
        "class names, and call edges between them."
    )
    args_schema: type[BaseModel] = GetCallGraphInput
    neo4j_tool: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, execution_flow_key: str) -> str:
        result = _run_async(self.neo4j_tool.get_call_graph(execution_flow_key))
        snippets = []
        for s in result.snippets.values():
            snippets.append({
                "key": s.key,
                "function_name": s.function_name or s.name,
                "file_path": s.file_path or "",
                "class_name": s.class_name or "",
                "type": s.type or "",
            })
        edges = []
        for e in result.edges:
            caller = result.get_snippet(e.caller_id)
            callee = result.get_snippet(e.callee_id)
            edges.append({
                "caller": caller.function_name or caller.name if caller else e.caller_id,
                "callee": callee.function_name or callee.name if callee else e.callee_id,
                "caller_file": caller.file_path if caller else "",
                "callee_file": callee.file_path if callee else "",
                "caller_class": caller.class_name if caller else "",
                "callee_class": callee.class_name if callee else "",
            })
        return json.dumps({"snippets": snippets, "edges": edges}, indent=2)

    async def _arun(self, execution_flow_key: str) -> str:
        result = await self.neo4j_tool.get_call_graph(execution_flow_key)
        snippets = []
        for s in result.snippets.values():
            snippets.append({
                "key": s.key,
                "function_name": s.function_name or s.name,
                "file_path": s.file_path or "",
                "class_name": s.class_name or "",
                "type": s.type or "",
            })
        edges = []
        for e in result.edges:
            caller = result.get_snippet(e.caller_id)
            callee = result.get_snippet(e.callee_id)
            edges.append({
                "caller": caller.function_name or caller.name if caller else e.caller_id,
                "callee": callee.function_name or callee.name if callee else e.callee_id,
                "caller_file": caller.file_path if caller else "",
                "callee_file": callee.file_path if callee else "",
                "caller_class": caller.class_name if caller else "",
                "callee_class": callee.class_name if callee else "",
            })
        return json.dumps({"snippets": snippets, "edges": edges}, indent=2)


class GetRootSnippetInput(BaseModel):
    execution_flow_key: str = Field(description="The execution flow key")


class Neo4jGetRootSnippetTool(LCBaseTool):
    """Find the ROOT snippet (entry point) for an execution flow in Neo4j."""

    name: str = "neo4j_get_root_snippet"
    description: str = (
        "Find the ROOT snippet (the entry-point function where the execution flow starts) "
        "for a given execution flow key. Returns function name, file path, class name."
    )
    args_schema: type[BaseModel] = GetRootSnippetInput
    neo4j_tool: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, execution_flow_key: str) -> str:
        result = _run_async(self.neo4j_tool.get_root_snippet(execution_flow_key))
        if not result:
            return json.dumps({"error": "No ROOT snippet found for this flow"})
        return json.dumps({
            "function_name": result.function_name or result.name or result.id,
            "file_path": result.file_path or "",
            "class_name": result.class_name or "",
            "type": result.type or "",
            "key": result.key,
        })

    async def _arun(self, execution_flow_key: str) -> str:
        result = await self.neo4j_tool.get_root_snippet(execution_flow_key)
        if not result:
            return json.dumps({"error": "No ROOT snippet found for this flow"})
        return json.dumps({
            "function_name": result.function_name or result.name or result.id,
            "file_path": result.file_path or "",
            "class_name": result.class_name or "",
            "type": result.type or "",
            "key": result.key,
        })


class GetExecutionFlowsInput(BaseModel):
    project_id: int = Field(description="The project ID")
    run_id: int = Field(description="The run ID")


class Neo4jGetExecutionFlowsTool(LCBaseTool):
    """List all execution flows for a project/run from Neo4j."""

    name: str = "neo4j_get_execution_flows"
    description: str = (
        "List all execution flow nodes for a given project_id and run_id from Neo4j. "
        "Returns a list of flow keys and names."
    )
    args_schema: type[BaseModel] = GetExecutionFlowsInput
    neo4j_tool: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, project_id: int, run_id: int) -> str:
        result = _run_async(
            self.neo4j_tool.get_execution_flows_by_project(project_id, run_id)
        )
        flows = [{"key": f.key, "name": f.name} for f in result]
        return json.dumps(flows, indent=2)

    async def _arun(self, project_id: int, run_id: int) -> str:
        result = await self.neo4j_tool.get_execution_flows_by_project(project_id, run_id)
        flows = [{"key": f.key, "name": f.name} for f in result]
        return json.dumps(flows, indent=2)


# ---------------------------------------------------------------------------
# Code Index tools
# ---------------------------------------------------------------------------


class SearchFunctionInput(BaseModel):
    function_name: str = Field(description="Function name to search for (case-insensitive)")
    class_name: Optional[str] = Field(default=None, description="Optional class name filter")


class CodeIndexSearchFunctionTool(LCBaseTool):
    """Search for a function by name in the code index."""

    name: str = "code_index_search_function"
    description: str = (
        "Search the indexed codebase for a function by name. Returns all matching "
        "entries with file path, class name, and function name. Use this to find "
        "where a function is defined in the codebase."
    )
    args_schema: type[BaseModel] = SearchFunctionInput
    code_index: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, function_name: str, class_name: str | None = None) -> str:
        conn = self.code_index._get_conn()
        query = "SELECT function_name, file_path, class_name FROM code_index WHERE UPPER(function_name) = ?"
        params = [function_name.upper().strip()]
        if class_name:
            query += " AND UPPER(class_name) = ?"
            params.append(class_name.upper().strip())
        rows = conn.execute(query, params).fetchall()
        results = [{"function_name": r[0], "file_path": r[1], "class_name": r[2]} for r in rows]
        return json.dumps(results, indent=2)

    async def _arun(self, function_name: str, class_name: str | None = None) -> str:
        return self._run(function_name, class_name)


class GetCallEdgesInput(BaseModel):
    caller_name: str = Field(description="The caller function name to get outgoing edges for")


class CodeIndexGetCallEdgesTool(LCBaseTool):
    """Get all outgoing call edges from a function in the code index."""

    name: str = "code_index_get_call_edges"
    description: str = (
        "Get all outgoing call edges from a given caller function in the indexed codebase. "
        "Returns the callees with file paths and confidence scores."
    )
    args_schema: type[BaseModel] = GetCallEdgesInput
    code_index: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, caller_name: str) -> str:
        all_edges = self.code_index.get_edges()
        results = []
        target = caller_name.upper().strip()
        for e in all_edges:
            raw_caller = e.get("from") or e.get("caller", "")
            parts = raw_caller.split("::")
            func_name = ""
            for part in reversed(parts):
                stripped = part.strip()
                if stripped:
                    func_name = stripped
                    break
            if func_name.upper().strip() == target:
                raw_callee = e.get("to") or e.get("callee", "")
                callee_parts = raw_callee.split("::")
                callee_name = ""
                for part in reversed(callee_parts):
                    stripped = part.strip()
                    if stripped:
                        callee_name = stripped
                        break
                results.append({
                    "caller_raw": raw_caller,
                    "callee_raw": raw_callee,
                    "callee_name": callee_name,
                    "confidence": e.get("confidence", 0.8),
                })
        return json.dumps(results, indent=2)

    async def _arun(self, caller_name: str) -> str:
        return self._run(caller_name)


class ListIndexFunctionsInput(BaseModel):
    limit: int = Field(default=50, description="Max number of functions to return")
    project_prefix: Optional[str] = Field(
        default=None,
        description="Optional project directory prefix to filter by",
    )


class CodeIndexListFunctionsTool(LCBaseTool):
    """List functions in the code index, optionally filtered by project prefix."""

    name: str = "code_index_list_functions"
    description: str = (
        "List functions stored in the code index. Optionally filter by a project "
        "directory prefix. Use this to understand what functions are available for "
        "a given project scope."
    )
    args_schema: type[BaseModel] = ListIndexFunctionsInput
    code_index: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, limit: int = 50, project_prefix: str | None = None) -> str:
        conn = self.code_index._get_conn()
        rows = conn.execute(
            "SELECT function_name, file_path, class_name FROM code_index"
        ).fetchall()
        results = []
        for fn, fp, cn in rows:
            if project_prefix:
                normalized = fp.replace("\\", "/")
                if not normalized.upper().startswith(project_prefix.upper()):
                    continue
            results.append({"function_name": fn, "file_path": fp, "class_name": cn})
            if len(results) >= limit:
                break
        return json.dumps(results, indent=2)

    async def _arun(self, limit: int = 50, project_prefix: str | None = None) -> str:
        return self._run(limit, project_prefix)


# ---------------------------------------------------------------------------
# Chunk retrieval tool (preferred over raw filesystem reads)
# ---------------------------------------------------------------------------


class GetFunctionChunkInput(BaseModel):
    function_name: str = Field(description="Function name to retrieve the code chunk for")
    class_name: Optional[str] = Field(default=None, description="Optional class name filter")


class CodeIndexGetFunctionChunkTool(LCBaseTool):
    """Retrieve the source code chunk for a function from the indexed codebase."""

    name: str = "code_index_get_function_chunk"
    description: str = (
        "Retrieve the actual source code of a function from the indexed codebase. "
        "Looks up the function in the code index to find its local file path, then "
        "extracts just the function body (not the whole file). This is the preferred "
        "way to read function source code — do NOT use filesystem_read_file for this."
    )
    args_schema: type[BaseModel] = GetFunctionChunkInput
    code_index: Any = None
    codebase_root: Any = None  # Path

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, function_name: str, class_name: str | None = None) -> str:
        return self._get_chunk(function_name, class_name)

    async def _arun(self, function_name: str, class_name: str | None = None) -> str:
        return self._get_chunk(function_name, class_name)

    def _get_chunk(self, function_name: str, class_name: str | None) -> str:
        from pathlib import Path

        from trustbot.indexing.chunker import chunk_file

        conn = self.code_index._get_conn()
        query = (
            "SELECT function_name, file_path, class_name FROM code_index "
            "WHERE UPPER(function_name) = ?"
        )
        params = [function_name.upper().strip()]
        if class_name:
            query += " AND UPPER(class_name) = ?"
            params.append(class_name.upper().strip())
        rows = conn.execute(query, params).fetchall()

        if not rows:
            return json.dumps({
                "error": f"Function '{function_name}' not found in code index",
                "suggestion": "Try code_index_search_function with a partial name",
            })

        root = Path(self.codebase_root) if self.codebase_root else Path(".")
        results = []
        for row in rows[:3]:  # Cap at 3 matches to avoid huge responses
            file_path = row[1]
            full_path = root / file_path
            if not full_path.exists():
                results.append({
                    "function_name": row[0],
                    "file_path": file_path,
                    "class_name": row[2],
                    "error": f"File not found at {full_path}",
                })
                continue

            chunks = chunk_file(full_path, root)
            target_upper = function_name.upper().strip()
            matched_chunk = None
            for chunk in chunks:
                if chunk.function_name.upper().strip() == target_upper:
                    if class_name:
                        if chunk.class_name.upper().strip() == class_name.upper().strip():
                            matched_chunk = chunk
                            break
                    else:
                        matched_chunk = chunk
                        break

            if matched_chunk:
                content = matched_chunk.content
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated, function too long)"
                results.append({
                    "function_name": matched_chunk.function_name,
                    "file_path": file_path,
                    "class_name": matched_chunk.class_name,
                    "line_start": matched_chunk.line_start,
                    "line_end": matched_chunk.line_end,
                    "language": matched_chunk.language,
                    "content": content,
                })
            else:
                results.append({
                    "function_name": row[0],
                    "file_path": file_path,
                    "class_name": row[2],
                    "error": "Function found in index but chunk extraction failed",
                })

        return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Filesystem tools (kept for text search; file reads replaced by chunk tool)
# ---------------------------------------------------------------------------


class ReadFileInput(BaseModel):
    file_path: str = Field(description="Relative or absolute path to the file")


class FilesystemReadFileTool(LCBaseTool):
    """Read a source file from the codebase."""

    name: str = "filesystem_read_file"
    description: str = (
        "Read the contents of a source file from the codebase. Provide the file path "
        "(relative to codebase root or absolute). Returns the file contents."
    )
    args_schema: type[BaseModel] = ReadFileInput
    filesystem_tool: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, file_path: str) -> str:
        try:
            result = _run_async(self.filesystem_tool.call("read_file", path=file_path))
            return result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _arun(self, file_path: str) -> str:
        try:
            result = await self.filesystem_tool.call("read_file", path=file_path)
            return result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})


class ExtractFunctionInput(BaseModel):
    file_path: str = Field(description="Path to the source file")
    function_name: str = Field(description="Name of the function to extract")


class FilesystemExtractFunctionTool(LCBaseTool):
    """Extract a function body from a source file."""

    name: str = "filesystem_extract_function"
    description: str = (
        "Extract the body of a specific function from a source file. Provide the file "
        "path and function name. Returns the function's source code."
    )
    args_schema: type[BaseModel] = ExtractFunctionInput
    filesystem_tool: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, file_path: str, function_name: str) -> str:
        try:
            result = _run_async(
                self.filesystem_tool.call(
                    "extract_function_body", path=file_path, function_name=function_name
                )
            )
            return result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _arun(self, file_path: str, function_name: str) -> str:
        try:
            result = await self.filesystem_tool.call(
                "extract_function_body", path=file_path, function_name=function_name
            )
            return result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})


class SearchTextInput(BaseModel):
    query: str = Field(description="Text to search for across all code files")


class FilesystemSearchTextTool(LCBaseTool):
    """Search for text across all code files in the codebase."""

    name: str = "filesystem_search_text"
    description: str = (
        "Search for a text string across all source files in the codebase. "
        "Returns file paths and line numbers where the text was found. "
        "Use this to find function calls, references, or definitions."
    )
    args_schema: type[BaseModel] = SearchTextInput
    filesystem_tool: Any = None

    model_config = {"arbitrary_types_allowed": True}

    def _run(self, query: str) -> str:
        try:
            result = _run_async(self.filesystem_tool.call("search_text", query=query))
            return json.dumps(result, default=str) if not isinstance(result, str) else result
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _arun(self, query: str) -> str:
        try:
            result = await self.filesystem_tool.call("search_text", query=query)
            return json.dumps(result, default=str) if not isinstance(result, str) else result
        except Exception as e:
            return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Factory: build tool sets for each agent
# ---------------------------------------------------------------------------


def build_neo4j_tools(neo4j_tool: Neo4jTool) -> list[LCBaseTool]:
    """Build LangChain tools for the Neo4j agent."""
    return [
        Neo4jGetCallGraphTool(neo4j_tool=neo4j_tool),
        Neo4jGetRootSnippetTool(neo4j_tool=neo4j_tool),
        Neo4jGetExecutionFlowsTool(neo4j_tool=neo4j_tool),
    ]


def build_codebase_tools(
    code_index: CodeIndex,
    filesystem_tool=None,
) -> list[LCBaseTool]:
    """Build LangChain tools for the codebase agent."""
    from trustbot.config import settings

    tools: list[LCBaseTool] = [
        CodeIndexSearchFunctionTool(code_index=code_index),
        CodeIndexGetCallEdgesTool(code_index=code_index),
        CodeIndexListFunctionsTool(code_index=code_index),
        CodeIndexGetFunctionChunkTool(
            code_index=code_index,
            codebase_root=settings.codebase_root.resolve(),
        ),
    ]
    if filesystem_tool:
        tools.append(FilesystemSearchTextTool(filesystem_tool=filesystem_tool))
    return tools


def build_verification_tools(
    code_index: CodeIndex | None = None,
    filesystem_tool=None,
) -> list[LCBaseTool]:
    """Build LangChain tools for the verification agent."""
    from trustbot.config import settings

    tools: list[LCBaseTool] = []
    if code_index:
        tools.append(CodeIndexSearchFunctionTool(code_index=code_index))
        tools.append(CodeIndexGetFunctionChunkTool(
            code_index=code_index,
            codebase_root=settings.codebase_root.resolve(),
        ))
    if filesystem_tool:
        tools.append(FilesystemSearchTextTool(filesystem_tool=filesystem_tool))
    return tools
