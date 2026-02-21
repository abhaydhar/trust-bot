"""
Normalization Agent — resolves function identities to canonical form.

Transforms edges from both Agent 1 and Agent 2 before comparison:
- Trim whitespace, normalize to uppercase
- Resolve aliases via configurable alias table
- Normalize file paths (absolute → filename only)
- Carry class_name through for comparison
"""

from __future__ import annotations

import logging

from trustbot.models.agentic import (
    AliasTable,
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
    normalize_file_path,
)

logger = logging.getLogger("trustbot.agents.normalization")


class NormalizationAgent:
    """Normalizes call graph edges for comparison."""

    def __init__(self, alias_table: AliasTable | None = None) -> None:
        self._aliases = alias_table or AliasTable()

    def normalize(self, output: CallGraphOutput) -> CallGraphOutput:
        """Apply normalization to all edges."""
        normalized_edges: list[CallGraphEdge] = []
        for e in output.edges:
            caller = self._normalize_name(e.caller)
            callee = self._normalize_name(e.callee)
            normalized_edges.append(
                CallGraphEdge(
                    caller=caller,
                    callee=callee,
                    caller_file=normalize_file_path(e.caller_file),
                    callee_file=normalize_file_path(e.callee_file),
                    caller_class=e.caller_class.upper().strip(),
                    callee_class=e.callee_class.upper().strip(),
                    depth=e.depth,
                    extraction_method=e.extraction_method,
                    confidence=e.confidence,
                )
            )

        return CallGraphOutput(
            execution_flow_id=output.execution_flow_id,
            source=output.source,
            root_function=self._normalize_name(output.root_function),
            edges=normalized_edges,
            unresolved_callees=[self._normalize_name(u) for u in output.unresolved_callees],
            metadata=output.metadata,
        )

    def _normalize_name(self, name: str) -> str:
        """Trim, uppercase, resolve aliases."""
        trimmed = name.strip()
        resolved = self._aliases.resolve(trimmed)
        return resolved.upper()
