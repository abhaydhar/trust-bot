"""
Pydantic models for Knowledge Graph Coverage Analysis.

Represents the full Neo4j node inventory and codebase-vs-KG coverage results.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Neo4jNodeInfo(BaseModel):
    """A single node from Neo4j with its label and key properties."""

    key: str = ""
    label: str = ""
    function_name: str = ""
    name: str = ""
    class_name: str = ""
    file_path: str = ""
    file_name: str = ""
    node_type: str = ""


class NodeTypeCount(BaseModel):
    """Count of nodes for a specific Neo4j label."""

    label: str
    count: int


class FileKGInventory(BaseModel):
    """Neo4j node inventory for a single file."""

    file_name: str
    file_path: str
    node_counts: dict[str, int] = Field(default_factory=dict)
    total_nodes: int = 0
    nodes: list[Neo4jNodeInfo] = Field(default_factory=list)


class FunctionCoverage(BaseModel):
    """Coverage status for a single codebase function."""

    function_name: str
    file_path: str
    class_name: str = ""
    status: str = "uncovered"
    match_tier: str = "none"
    matched_node_label: str = ""


class FileCoverage(BaseModel):
    """Coverage status for a single codebase file."""

    file_path: str
    file_name: str
    language: str = ""
    total_functions: int = 0
    covered_functions: int = 0
    uncovered_functions: int = 0
    status: str = "uncovered"
    functions: list[FunctionCoverage] = Field(default_factory=list)


class KGCoverageResult(BaseModel):
    """Full result of the KG coverage analysis."""

    project_id: int
    run_id: int

    # KG Inventory
    node_type_summary: list[NodeTypeCount] = Field(default_factory=list)
    total_neo4j_nodes: int = 0
    files_in_neo4j: list[FileKGInventory] = Field(default_factory=list)

    # Coverage comparison
    total_codebase_files: int = 0
    covered_files: int = 0
    partial_files: int = 0
    uncovered_files: int = 0
    total_codebase_functions: int = 0
    covered_functions: int = 0
    uncovered_functions: int = 0
    file_coverage_pct: float = 0.0
    function_coverage_pct: float = 0.0
    file_coverages: list[FileCoverage] = Field(default_factory=list)
