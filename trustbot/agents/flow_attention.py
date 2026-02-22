"""
Flow attention analysis — explains why flows have phantom/missing edges and suggests fixes.

Analyzes VerificationResult plus optional Neo4j and Index graphs to produce:
- Per-edge reasons (qualified vs bare name, root not in index, etc.)
- Root resolution status from Agent 2
- Actionable fix suggestions (index naming, normalization, scoping).
"""

from __future__ import annotations


def _to_bare(name: str) -> str:
    s = (name or "").strip().upper()
    if "." in s:
        return s.rsplit(".", 1)[-1].strip()
    return s


def _is_qualified(name: str) -> bool:
    return "." in (name or "").strip()


def analyze_flow_attention(
    result,
    neo4j_graph=None,
    index_graph=None,
) -> dict:
    """
    Produce a structured analysis of why a flow requires attention (phantom/missing edges).

    Args:
        result: VerificationResult from the verification agent.
        neo4j_graph: Optional CallGraphOutput from Agent 1 (for context).
        index_graph: Optional CallGraphOutput from Agent 2 (for root/index metadata).

    Returns:
        Dict with keys:
          - phantom_reasons: list of {caller, callee, reason, fix_suggestion}
          - missing_reasons: list of {caller, callee, reason, fix_suggestion}
          - root_analysis: {found_in_index, has_outgoing_edges, message}
          - likely_causes: list of short summary strings
          - fix_suggestions: list of actionable recommendations
    """
    phantom_reasons = []
    missing_reasons = []
    root_analysis = {}
    likely_causes = []
    fix_suggestions = []

    # Index graph metadata (Agent 2)
    idx_meta = getattr(index_graph, "metadata", None) if index_graph else {}
    idx_meta = idx_meta or {}
    root_found = idx_meta.get("root_found_in_index")
    root_has_edges = idx_meta.get("root_has_outgoing_edges")
    sample_index = idx_meta.get("sample_index_functions") or []
    sample_edge_callers = idx_meta.get("sample_edge_callers") or []
    resolved_via = idx_meta.get("resolved_via", "original")

    # Root analysis
    if root_found is not None:
        root_analysis["found_in_index"] = root_found
        root_analysis["has_outgoing_edges"] = root_has_edges
        if not root_found:
            root_analysis["message"] = (
                "Root function from Neo4j was not found in the code index. "
                "Agent 2 looks up the root by name; index stores bare function names."
            )
            likely_causes.append("Root not in index (name or project scope)")
            fix_suggestions.append(
                "Ensure the code index was built from the same project. "
                "If Neo4j uses a qualified name (e.g. TForm1), Agent 2 tries class fallback; "
                "check that the index contains the same file/class."
            )
        elif root_has_edges is False:
            root_analysis["message"] = (
                "Root was found in the index but has no outgoing call edges stored. "
                "Call graph extraction may have missed calls from this function."
            )
            likely_causes.append("Root has no outgoing edges in index")
            fix_suggestions.append(
                "Rebuild the call graph (Code Indexer) so that calls from the root are extracted. "
                "Check call_graph_builder patterns for the language (e.g. Delphi bare calls)."
            )
        else:
            root_analysis["message"] = f"Root resolved via: {resolved_via}."

    # Analyze phantom edges (Neo4j has it, index does not)
    for e in result.phantom_edges:
        caller, callee = e.caller, e.callee
        reason = "Not found in indexed codebase."
        fix = ""

        if _is_qualified(caller) or _is_qualified(callee):
            bare_caller = _to_bare(caller)
            bare_callee = _to_bare(callee)
            reason = (
                "Neo4j uses qualified names (e.g. Class.Method) while the index stores "
                "bare function names. So Neo4j edge may be the same as an index edge "
                f"under bare names ({bare_caller} → {bare_callee})."
            )
            fix = (
                "Verification now matches on bare names for name-only tier. If this still appears, "
                "the index may not contain this call (wrong project scope or extraction gap)."
            )
            if "qualified" not in str(likely_causes):
                likely_causes.append("Qualified vs bare name (Neo4j Class.Method vs index Method)")
        else:
            reason = (
                "This edge exists in Neo4j but no matching edge was found in the index. "
                "Possible causes: index built from different scope, call not extracted (e.g. dynamic)."
            )
            fix = "Rebuild index from the correct folder; ensure call_graph_builder patterns cover this call style."

        phantom_reasons.append({
            "caller": caller,
            "callee": callee,
            "reason": reason,
            "fix_suggestion": fix or "Check index project scope and call extraction.",
        })

    # Analyze missing edges (index has it, Neo4j does not)
    for e in result.missing_edges:
        caller, callee = e.caller, e.callee
        reason = "In index but not in Neo4j graph."
        fix = ""

        if not _is_qualified(caller) and not _is_qualified(callee):
            reason = (
                "Index uses bare names. Neo4j may use qualified names (Class.Method); "
                "the same logical edge might appear in Neo4j under a qualified caller/callee."
            )
            fix = "Verification matches on bare names; if still missing, Neo4j flow may not include this call."
        else:
            reason = "Index edge was not present in the Neo4j execution flow. Flow coverage may differ."
            fix = "Confirm whether this call is expected in the execution flow; Neo4j flow might be filtered or partial."

        missing_reasons.append({
            "caller": caller,
            "callee": callee,
            "reason": reason,
            "fix_suggestion": fix or "Compare flow scope between Neo4j and index.",
        })

    if result.missing_edges and "Index-only edges" not in str(likely_causes):
        likely_causes.append("Index-only edges (index has more calls than Neo4j flow)")

    # Aggregate fix suggestions (dedupe by key phrase)
    seen = set()
    for s in fix_suggestions:
        key = s[:60]
        if key not in seen:
            seen.add(key)
    # Add generic if we have phantom/missing but no root issue
    if (result.phantom_edges or result.missing_edges) and not root_analysis:
        fix_suggestions.append(
            "Agent 2 picks function names from the code index (chunk_id = path::class::function). "
            "Ensure the index was built from the same project and that call_edges use the same naming."
        )

    return {
        "phantom_reasons": phantom_reasons,
        "missing_reasons": missing_reasons,
        "root_analysis": root_analysis,
        "likely_causes": likely_causes,
        "fix_suggestions": list(dict.fromkeys(fix_suggestions)),
    }


def format_flow_attention_markdown(analysis: dict, flow_name: str = "", max_phantom: int = 15, max_missing: int = 15) -> str:
    """Format the analysis as markdown for the UI."""
    lines = []
    if flow_name:
        lines.append(f"#### Flow: {flow_name}")
        lines.append("")

    root = analysis.get("root_analysis") or {}
    if root:
        lines.append("**Root (Agent 2)**")
        lines.append(f"- Found in index: {root.get('found_in_index', '—')}")
        lines.append(f"- Has outgoing edges: {root.get('has_outgoing_edges', '—')}")
        if root.get("message"):
            lines.append(f"- {root['message']}")
        lines.append("")

    causes = analysis.get("likely_causes") or []
    if causes:
        lines.append("**Likely causes**")
        for c in causes:
            lines.append(f"- {c}")
        lines.append("")

    phantom = analysis.get("phantom_reasons") or []
    if phantom:
        lines.append("**Phantom edges (Neo4j only) — why they don’t match**")
        for r in phantom[:max_phantom]:
            lines.append(f"- `{r['caller']}` → `{r['callee']}`: {r['reason']}")
            if r.get("fix_suggestion"):
                lines.append(f"  - *Fix:* {r['fix_suggestion']}")
        if len(phantom) > max_phantom:
            lines.append(f"- … and {len(phantom) - max_phantom} more.")
        lines.append("")

    missing = analysis.get("missing_reasons") or []
    if missing:
        lines.append("**Missing edges (index only) — why they don’t match**")
        for r in missing[:max_missing]:
            lines.append(f"- `{r['caller']}` → `{r['callee']}`: {r['reason']}")
            if r.get("fix_suggestion"):
                lines.append(f"  - *Fix:* {r['fix_suggestion']}")
        if len(missing) > max_missing:
            lines.append(f"- … and {len(missing) - max_missing} more.")
        lines.append("")

    fixes = analysis.get("fix_suggestions") or []
    if fixes:
        lines.append("**Recommended actions**")
        for f in fixes:
            lines.append(f"- {f}")
        lines.append("")

    return "\n".join(lines) if lines else "No analysis available."
