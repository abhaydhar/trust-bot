from __future__ import annotations

from pydantic import BaseModel, Field


class ExecutionFlow(BaseModel):
    """A logical execution path retrieved from Neo4j."""

    key: str
    name: str = ""
    description: str = ""
    project_id: int | None = None
    run_id: int | None = None
    module_name: str = ""
    flow_type: str = ""
    complexity: str = ""
    properties: dict = Field(default_factory=dict)


class Snippet(BaseModel):
    """KB representation of a single function/method in the codebase."""

    id: str
    key: str = ""
    function_name: str = ""
    name: str = ""
    class_name: str = ""
    file_path: str = ""
    file_name: str = ""
    tech: str = ""
    line_start: int | None = None
    line_end: int | None = None
    snippet_code: str = ""
    type: str = ""
    module_name: str = ""
    starts_flow: bool = False
    properties: dict = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.function_name or self.name or self.id


class CallEdge(BaseModel):
    """A directed call relationship between two Snippets."""

    caller_id: str
    callee_id: str
    relationship_type: str = "CALLS"
    properties: dict = Field(default_factory=dict)


class CallGraph(BaseModel):
    """Complete call graph for a single execution flow."""

    execution_flow: ExecutionFlow
    snippets: dict[str, Snippet] = Field(default_factory=dict)
    edges: list[CallEdge] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)

    def get_snippet(self, snippet_id: str) -> Snippet | None:
        return self.snippets.get(snippet_id)

    def get_callers_of(self, snippet_id: str) -> list[CallEdge]:
        return [e for e in self.edges if e.callee_id == snippet_id]

    def get_callees_of(self, snippet_id: str) -> list[CallEdge]:
        return [e for e in self.edges if e.caller_id == snippet_id]


class ProjectCallGraph(BaseModel):
    """Combined call graphs for all execution flows in a project run."""

    project_id: int
    run_id: int
    execution_flows: list[ExecutionFlow] = Field(default_factory=list)
    call_graphs: list[CallGraph] = Field(default_factory=list)

    @property
    def all_snippets(self) -> dict[str, Snippet]:
        merged: dict[str, Snippet] = {}
        for cg in self.call_graphs:
            merged.update(cg.snippets)
        return merged

    @property
    def all_edges(self) -> list[CallEdge]:
        edges: list[CallEdge] = []
        for cg in self.call_graphs:
            edges.extend(cg.edges)
        return edges

    @property
    def total_snippets(self) -> int:
        return len(self.all_snippets)

    @property
    def total_edges(self) -> int:
        return sum(len(cg.edges) for cg in self.call_graphs)
