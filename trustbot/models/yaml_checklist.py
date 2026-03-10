"""Pydantic models for YAML Checklist validation report."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChecklistItemResult(BaseModel):
    """Result of validating a single checklist item."""

    item_id: int
    title: str
    category: str  # Nodes | Relationships | Execution Order | Connectivity
    priority: str  # Critical | Required
    passed: bool
    failed_keys: list[str] = Field(default_factory=list)
    details: str = ""


class YamlChecklistReport(BaseModel):
    """Full YAML checklist validation report."""

    project_id: int
    run_id: int
    items: list[ChecklistItemResult] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
