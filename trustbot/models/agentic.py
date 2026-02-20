"""
Shared models for the dual-agent call graph validation architecture.

Both Agent 1 (Neo4j) and Agent 2 (Filesystem) emit graphs in this identical format,
enabling direct comparison by the Verification Agent.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class GraphSource(str, Enum):
    """Identifies which agent produced the graph."""

    NEO4J = "neo4j"
    FILESYSTEM = "filesystem"


class ExtractionMethod(str, Enum):
    """How the edge was extracted."""

    NEO4J = "neo4j"
    REGEX = "regex"
    LLM_TIER2 = "llm_tier2"
    LLM_TIER3 = "llm_tier3"


class EdgeClassification(str, Enum):
    """Verification agent classification of an edge."""

    CONFIRMED = "confirmed"
    PHANTOM = "phantom"  # In Neo4j only
    MISSING = "missing"  # In filesystem only
    CONFLICTED = "conflicted"  # In both but different targets


class CallGraphEdge(BaseModel):
    """Single edge in the shared call graph format."""

    caller: str
    callee: str
    caller_file: str = ""
    callee_file: str = ""
    depth: int = 1
    extraction_method: ExtractionMethod = ExtractionMethod.NEO4J
    confidence: float = 1.0


class CallGraphOutput(BaseModel):
    """Shared output format emitted by both Agent 1 and Agent 2."""

    execution_flow_id: str
    source: GraphSource
    root_function: str
    edges: list[CallGraphEdge] = Field(default_factory=list)
    unresolved_callees: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    def to_comparable_edges(self) -> set[tuple[str, str]]:
        """Return set of (caller, callee) for diffing."""
        return {(e.caller.upper().strip(), e.callee.upper().strip()) for e in self.edges}


class SpecFlowDocument(BaseModel):
    """Input for Agent 2 — root function and file path for traversal."""

    root_function: str
    root_file_path: str
    language: str = "python"
    execution_flow_id: str = ""


class ValidationJobPayload(BaseModel):
    """Payload for a single validation job in the queue."""

    job_id: str
    execution_flow_id: str
    spec_flow_document: SpecFlowDocument


class VerifiedEdge(BaseModel):
    """Edge with verification result."""

    caller: str
    callee: str
    classification: EdgeClassification
    trust_score: float
    caller_file: str = ""
    callee_file: str = ""
    details: str = ""


class VerificationResult(BaseModel):
    """Output from the Verification Agent."""

    execution_flow_id: str
    graph_trust_score: float
    flow_trust_score: float
    confirmed_edges: list[VerifiedEdge] = Field(default_factory=list)
    phantom_edges: list[VerifiedEdge] = Field(default_factory=list)
    missing_edges: list[VerifiedEdge] = Field(default_factory=list)
    conflicted_edges: list[VerifiedEdge] = Field(default_factory=list)
    unresolved_callees: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class AliasEntry(BaseModel):
    """Canonical name and its aliases for normalization."""

    canonical: str
    aliases: list[str] = Field(default_factory=list)


class AliasTable(BaseModel):
    """Configurable alias table for function identity resolution."""

    aliases: list[AliasEntry] = Field(default_factory=list)

    def resolve(self, name: str) -> str:
        """Resolve a name to its canonical form."""
        upper = name.upper().strip()
        for entry in self.aliases:
            if upper == entry.canonical.upper():
                return entry.canonical
            if upper in [a.upper() for a in entry.aliases]:
                return entry.canonical
        return upper
