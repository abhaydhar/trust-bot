"""
Report Agent — generates validation report (Markdown/HTML) and Neo4j writeback.

Produces human-readable reports and optionally writes trust scores back to Neo4j.
"""

from __future__ import annotations

import logging
from datetime import datetime

from trustbot.models.agentic import EdgeClassification, VerificationResult

logger = logging.getLogger("trustbot.agents.report")


class ReportAgent:
    """Generates validation reports from VerificationResult."""

    def generate_markdown(self, result: VerificationResult) -> str:
        """Generate a Markdown validation report."""
        meta = result.metadata
        lines = [
            f"# Validation Report: {result.execution_flow_id}",
            "",
            f"**Generated**: {datetime.utcnow().isoformat()}Z",
            "",
            "## Trust Scores",
            "",
            f"- **Flow Trust Score**: {result.flow_trust_score:.2%}",
            f"- **Graph Trust Score**: {result.graph_trust_score:.2%}",
            "",
            "## Summary",
            "",
            f"- **Confirmed edges**: {len(result.confirmed_edges)}",
        ]
        if meta.get("match_full") is not None:
            lines.append(f"  - Full match (name+class+file): {meta.get('match_full', 0)}")
            lines.append(f"  - Name+file match: {meta.get('match_name_file', 0)}")
            lines.append(f"  - Name-only match: {meta.get('match_name_only', 0)}")
        lines.extend([
            f"- **Phantom edges** (Neo4j only): {len(result.phantom_edges)}",
            f"- **Missing edges** (filesystem only): {len(result.missing_edges)}",
            f"- **Conflicted edges**: {len(result.conflicted_edges)}",
            f"- **Unresolved callees**: {len(result.unresolved_callees)}",
            "",
        ])

        if result.confirmed_edges:
            lines.extend(["## Confirmed Edges", ""])
            lines.append("| Caller | Callee | Trust | Match Type |")
            lines.append("|--------|--------|-------|------------|")
            for e in result.confirmed_edges[:30]:
                lines.append(f"| `{e.caller}` | `{e.callee}` | {e.trust_score:.2f} | {e.details} |")
            if len(result.confirmed_edges) > 30:
                lines.append(f"| ... | +{len(result.confirmed_edges) - 30} more | | |")
            lines.append("")

        if result.phantom_edges:
            lines.extend(["## Phantom Edges (Neo4j only)", ""])
            for e in result.phantom_edges[:20]:
                lines.append(f"- `{e.caller}` → `{e.callee}` (score: {e.trust_score:.2f})")
            if len(result.phantom_edges) > 20:
                lines.append(f"- ... and {len(result.phantom_edges) - 20} more")
            lines.append("")

        if result.missing_edges:
            lines.extend(["## Missing Edges (Filesystem only)", ""])
            for e in result.missing_edges[:20]:
                lines.append(f"- `{e.caller}` → `{e.callee}`")
            if len(result.missing_edges) > 20:
                lines.append(f"- ... and {len(result.missing_edges) - 20} more")
            lines.append("")

        if result.unresolved_callees:
            lines.extend(["## Unresolved Callees", ""])
            for u in result.unresolved_callees[:20]:
                lines.append(f"- `{u}`")
            lines.append("")

        # Execution order comparison
        order_mismatches = meta.get("execution_order_mismatches", [])
        order_matches = meta.get("execution_order_matches", 0)
        if order_mismatches or order_matches:
            lines.extend(["## Execution Order", ""])
            lines.append(
                f"- **Callers with correct order**: {order_matches}"
            )
            if order_mismatches:
                lines.append(
                    f"- **Callers with order mismatch**: {len(order_mismatches)}"
                )
                lines.append("")
                for m in order_mismatches[:10]:
                    lines.append(f"**`{m['caller']}`**:")
                    lines.append(f"  - Neo4j order: {' -> '.join(m['neo4j_order'])}")
                    lines.append(f"  - Index order:  {' -> '.join(m['index_order'])}")
                    lines.append("")
            else:
                lines.append("- All confirmed callers have matching execution order")
                lines.append("")

        return "\n".join(lines)

    def generate_summary(self, result: VerificationResult) -> str:
        """Short one-line summary for UI."""
        total = len(result.confirmed_edges) + len(result.phantom_edges) + len(result.missing_edges)
        if total == 0:
            return "No edges to validate."
        return (
            f"Flow {result.execution_flow_id}: "
            f"{result.flow_trust_score:.0%} trust — "
            f"{len(result.confirmed_edges)} confirmed, "
            f"{len(result.phantom_edges)} phantom, "
            f"{len(result.missing_edges)} missing"
        )
