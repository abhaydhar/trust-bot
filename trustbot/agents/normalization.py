"""
Normalization Agent — resolves function identities to canonical form.

Transforms edges from both Agent 1 and Agent 2 before comparison:
- Trim whitespace, normalize to uppercase
- Resolve aliases via configurable alias table
- Strip language-specific prefixes/suffixes
"""

from __future__ import annotations

import logging

from trustbot.models.agentic import (
    AliasTable,
    CallGraphEdge,
    CallGraphOutput,
    ExtractionMethod,
    GraphSource,
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
                    caller_file=e.caller_file,
                    callee_file=e.callee_file,
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
