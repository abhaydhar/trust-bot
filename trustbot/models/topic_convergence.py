"""Models for the Topic Convergence Agent.

Covers topic analysis results, issue classification, and audit/change records
for Neo4j node topic validation and remediation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TopicIssueType(str, Enum):
    DUPLICATE = "duplicate"
    SIMILAR = "similar"
    VERB_NOUN_VIOLATION = "verb_noun"
    MISALIGNED = "misaligned"
    JOURNEY_BREAK = "journey_break"
    TECHNICAL_GLUE = "technical_glue"
    TOPIC_MISSING = "topic_missing"


class NodeTopicAnalysis(BaseModel):
    """Analysis result for a single Neo4j node's topic field."""

    node_key: str
    node_name: str
    node_type: str
    parent_snippet_key: str | None = None
    execution_flow_key: str
    execution_flow_name: str
    current_topic: str = ""
    business_summary: str = ""
    issues: list[TopicIssueType] = Field(default_factory=list)
    issue_details: str = ""
    suggested_topic: str = ""
    suggestion_rationale: str = ""
    confidence: float = 0.0
    chain_position: int | None = None
    chain_context: str = ""
    duplicate_group_id: str | None = None


class TopicAnalysisReport(BaseModel):
    """Aggregate report from the Topic Convergence Agent."""

    project_id: int
    run_id: int
    total_nodes_analyzed: int = 0
    nodes_with_issues: int = 0
    nodes_missing_topic: int = 0
    issue_breakdown: dict[str, int] = Field(default_factory=dict)
    node_type_breakdown: dict[str, int] = Field(default_factory=dict)
    analyses: list[NodeTopicAnalysis] = Field(default_factory=list)
    duplicate_groups: dict[str, list[str]] = Field(default_factory=dict)
    journey_chains: dict[str, list[str]] = Field(default_factory=dict)


class TopicChangeRecord(BaseModel):
    """Audit entry for a single topic write-back."""

    node_key: str
    node_type: str
    node_label: str
    old_topic: str
    new_topic: str
    changed_by: str = "user"
    changed_at: datetime = Field(default_factory=datetime.utcnow)
    execution_flow_key: str = ""
    is_undo: bool = False
