"""
Call tree builder — generates text and Mermaid representations of call graphs.

Used by the report formatter to show call trees for Agent 1 and Agent 2.
"""

from __future__ import annotations

from trustbot.models.agentic import CallGraphEdge, CallGraphOutput, normalize_file_path


def _short_file(path: str) -> str:
    """Extract just the filename from a path."""
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def build_adjacency(edges: list[CallGraphEdge]) -> dict[str, list[str]]:
    """Build caller → [callees] adjacency map from edges."""
    adj: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for e in edges:
        key = (e.caller.upper(), e.callee.upper())
        if key not in seen:
            seen.add(key)
            adj.setdefault(e.caller, []).append(e.callee)
    return adj


def build_text_tree(graph: CallGraphOutput, label: str = "") -> str:
    """
    Build a text-based call tree using box-drawing characters.

    Example output:
        InitialiseEcran (fMain.pas)
        └── ChargeArborescence (fMain.pas)
            └── ChargeArborescence ↻ (recursive)
    """
    if not graph.edges:
        return f"*{label or graph.source.value}: No edges*"

    adj = build_adjacency(graph.edges)
    file_map: dict[str, str] = {}
    for e in graph.edges:
        if e.caller and e.caller_file:
            file_map[e.caller] = _short_file(e.caller_file)
        if e.callee and e.callee_file:
            file_map[e.callee] = _short_file(e.callee_file)

    root = graph.root_function
    if root not in adj:
        candidates = set()
        callees = set()
        for e in graph.edges:
            candidates.add(e.caller)
            callees.add(e.callee)
        roots = candidates - callees
        root = sorted(roots)[0] if roots else graph.edges[0].caller

    lines: list[str] = []
    visited: set[str] = set()

    def _walk(node: str, prefix: str, is_last: bool, depth: int):
        if depth > 15:
            return

        node_file = file_map.get(node, "")
        file_tag = f" ({node_file})" if node_file else ""

        if depth == 0:
            lines.append(f"[ROOT] {node}{file_tag}")
        else:
            connector = "`-- " if is_last else "|-- "
            if node.upper() in visited:
                lines.append(f"{prefix}{connector}{node} (recursive)")
                return
            lines.append(f"{prefix}{connector}{node}{file_tag}")

        visited.add(node.upper())
        children = adj.get(node, [])
        seen_children: list[str] = []
        for c in children:
            if c not in seen_children:
                seen_children.append(c)
        children = seen_children

        child_prefix = prefix + ("    " if is_last else "|   ") if depth > 0 else ""
        for i, child in enumerate(children):
            is_child_last = (i == len(children) - 1)
            _walk(child, child_prefix, is_child_last, depth + 1)

    _walk(root, "", True, 0)
    return "\n".join(lines)


def _sanitize_mermaid(text: str) -> str:
    """Remove or escape characters that break Mermaid syntax."""
    return (text
            .replace('"', "'")
            .replace("&", "and")
            .replace("<", "")
            .replace(">", "")
            .replace("#", "")
            .replace(";", ""))


def build_mermaid(
    graph: CallGraphOutput,
    label: str = "",
    direction: str = "TD",
) -> str:
    """
    Generate a Mermaid flowchart from a call graph.
    Deduplicates edges and sanitizes names for Mermaid syntax.
    """
    if not graph.edges:
        return ""

    file_map: dict[str, str] = {}
    for e in graph.edges:
        if e.caller and e.caller_file:
            file_map[e.caller] = _short_file(e.caller_file)
        if e.callee and e.callee_file:
            file_map[e.callee] = _short_file(e.callee_file)

    # Collect unique function names (deduplicated, case-insensitive)
    all_names: list[str] = []
    seen: set[str] = set()
    for e in graph.edges:
        for name in (e.caller, e.callee):
            if name.upper() not in seen:
                seen.add(name.upper())
                all_names.append(name)

    node_id: dict[str, str] = {}
    for i, name in enumerate(all_names):
        node_id[name.upper()] = f"N{i}"

    lines = [f"graph {direction}"]

    for name in all_names:
        nid = node_id[name.upper()]
        safe_name = _sanitize_mermaid(name)
        finfo = _sanitize_mermaid(file_map.get(name, ""))
        label = f"{safe_name} | {finfo}" if finfo else safe_name
        lines.append(f"    {nid}([{label}])")

    # Deduplicated edges, skip self-loops (they break Mermaid rendering)
    edge_seen: set[tuple[str, str]] = set()
    for e in graph.edges:
        if e.caller.upper() == e.callee.upper():
            continue
        ck = (e.caller.upper(), e.callee.upper())
        if ck in edge_seen:
            continue
        edge_seen.add(ck)
        src = node_id.get(e.caller.upper())
        tgt = node_id.get(e.callee.upper())
        if not src or not tgt:
            continue
        lines.append(f"    {src} --> {tgt}")

    # Style the root node green
    root_key = graph.root_function.upper()
    if root_key in node_id:
        rid = node_id[root_key]
        lines.append(f"    style {rid} fill:#4CAF50,color:#fff,stroke:#388E3C")

    return "\n".join(lines)


def build_mermaid_html(
    neo4j_graph: CallGraphOutput | None,
    index_graph: CallGraphOutput | None,
    flow_name: str = "",
    flow_idx: int = 0,
) -> str:
    """
    Build an HTML block with side-by-side Mermaid diagrams for Agent 1 and Agent 2.
    Uses Mermaid.js CDN for rendering.
    """
    neo4j_mermaid = build_mermaid(neo4j_graph, "Neo4j") if neo4j_graph else ""
    index_mermaid = build_mermaid(index_graph, "Index") if index_graph else ""

    if not neo4j_mermaid and not index_mermaid:
        return ""

    uid = f"flow_{flow_idx}"

    sections = []
    if neo4j_mermaid:
        sections.append(f"""
        <div style="flex:1;min-width:300px;">
            <h4 style="margin:0 0 8px;color:#FF9800;">Agent 1 — Neo4j Call Tree</h4>
            <div class="mermaid" id="{uid}_neo4j">{neo4j_mermaid}</div>
        </div>""")
    if index_mermaid:
        sections.append(f"""
        <div style="flex:1;min-width:300px;">
            <h4 style="margin:0 0 8px;color:#9C27B0;">Agent 2 — Index Call Tree</h4>
            <div class="mermaid" id="{uid}_index">{index_mermaid}</div>
        </div>""")

    return f"""
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin:12px 0;padding:12px;
                background:#fafafa;border:1px solid #e0e0e0;border-radius:8px;">
        {''.join(sections)}
    </div>"""
