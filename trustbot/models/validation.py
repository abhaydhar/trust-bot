from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class NodeVerdict(str, Enum):
    VALID = "VALID"
    DRIFTED = "DRIFTED"
    MISSING = "MISSING"


class EdgeVerdict(str, Enum):
    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"
    CONTRADICTED = "CONTRADICTED"


class NodeStatus(BaseModel):
    """Validation result for a single Snippet node."""

    snippet_id: str
    function_name: str
    file_path: str
    verdict: NodeVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    details: str = ""


class EdgeStatus(BaseModel):
    """Validation result for a single call edge."""

    caller_id: str
    callee_id: str
    caller_function: str = ""
    callee_function: str = ""
    verdict: EdgeVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    details: str = ""


class ValidationSummary(BaseModel):
    total_nodes: int = 0
    valid_nodes: int = 0
    drifted_nodes: int = 0
    missing_nodes: int = 0
    total_edges: int = 0
    confirmed_edges: int = 0
    unconfirmed_edges: int = 0
    contradicted_edges: int = 0


class ValidationReport(BaseModel):
    """Validation report for a single execution flow's call graph."""

    execution_flow_key: str
    execution_flow_name: str = ""
    node_results: list[NodeStatus] = Field(default_factory=list)
    edge_results: list[EdgeStatus] = Field(default_factory=list)
    summary: ValidationSummary = Field(default_factory=ValidationSummary)
    llm_summary: str = ""

    def compute_summary(self) -> None:
        self.summary = ValidationSummary(
            total_nodes=len(self.node_results),
            valid_nodes=sum(1 for n in self.node_results if n.verdict == NodeVerdict.VALID),
            drifted_nodes=sum(1 for n in self.node_results if n.verdict == NodeVerdict.DRIFTED),
            missing_nodes=sum(1 for n in self.node_results if n.verdict == NodeVerdict.MISSING),
            total_edges=len(self.edge_results),
            confirmed_edges=sum(
                1 for e in self.edge_results if e.verdict == EdgeVerdict.CONFIRMED
            ),
            unconfirmed_edges=sum(
                1 for e in self.edge_results if e.verdict == EdgeVerdict.UNCONFIRMED
            ),
            contradicted_edges=sum(
                1 for e in self.edge_results if e.verdict == EdgeVerdict.CONTRADICTED
            ),
        )


class ProjectValidationReport(BaseModel):
    """Aggregated validation report for all execution flows in a project run."""

    project_id: int
    run_id: int
    flow_reports: list[ValidationReport] = Field(default_factory=list)
    overall_summary: ValidationSummary = Field(default_factory=ValidationSummary)
    llm_summary: str = ""

    def compute_overall_summary(self) -> None:
        for r in self.flow_reports:
            r.compute_summary()
        self.overall_summary = ValidationSummary(
            total_nodes=sum(r.summary.total_nodes for r in self.flow_reports),
            valid_nodes=sum(r.summary.valid_nodes for r in self.flow_reports),
            drifted_nodes=sum(r.summary.drifted_nodes for r in self.flow_reports),
            missing_nodes=sum(r.summary.missing_nodes for r in self.flow_reports),
            total_edges=sum(r.summary.total_edges for r in self.flow_reports),
            confirmed_edges=sum(r.summary.confirmed_edges for r in self.flow_reports),
            unconfirmed_edges=sum(r.summary.unconfirmed_edges for r in self.flow_reports),
            contradicted_edges=sum(r.summary.contradicted_edges for r in self.flow_reports),
        )
