"""
Call tree builder -- generates text, Mermaid, and ECharts representations.

Used by the UI to show call trees for Agent 1 and Agent 2.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trustbot.models.agentic import CallGraphEdge, CallGraphOutput

logger = logging.getLogger(__name__)


def _short_file(path: str) -> str:
    """Extract just the filename from a path."""
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def build_adjacency(edges: list[CallGraphEdge]) -> dict[str, list[str]]:
    """Build caller -> [callees] adjacency map from edges, preserving execution_order."""
    sorted_edges = sorted(edges, key=lambda e: e.execution_order)
    adj: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for e in sorted_edges:
        key = (e.caller.upper(), e.callee.upper())
        if key not in seen:
            seen.add(key)
            adj.setdefault(e.caller, []).append(e.callee)
    return adj


def build_text_tree(graph: CallGraphOutput, label: str = "") -> str:
    """
    Build a text-based call tree using ASCII-safe characters.

    Example output::

        [ROOT] InitialiseEcran (fMain.pas)
        |-- ChargeArborescence (fMain.pas)
        |   `-- ChargeArborescence (recursive)
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


# ---------------------------------------------------------------------------
# Mermaid diagram generation
# ---------------------------------------------------------------------------

def _sanitize_mermaid(text: str) -> str:
    """Remove or escape characters that break Mermaid syntax."""
    if not text:
        return ""
    return (
        str(text)
        .replace("\\", "/")
        .replace('"', "'")
        .replace("&", "and")
        .replace("<", "")
        .replace(">", "")
        .replace("#", "")
        .replace(";", "")
        .replace("[", "")
        .replace("]", "")
        .replace("|", " - ")
        .replace("{", "")
        .replace("}", "")
        .strip()
    )


def _mermaid_quote(label: str) -> str:
    """Escape a label for use inside Mermaid double-quoted string."""
    if not label:
        return "?"
    # Inside "...", only " and \ need escaping
    return label.replace("\\", "\\\\").replace('"', '\\"')


def build_mermaid(graph: CallGraphOutput, direction: str = "TD") -> str:
    """
    Generate a Mermaid flowchart string from a call graph.
    Uses quoted node labels to avoid syntax errors from special chars and reserved words.
    Returns an empty string if the graph has no edges.
    """
    if not graph or not graph.edges:
        return ""

    file_map: dict[str, str] = {}
    for e in graph.edges:
        if e.caller and e.caller_file:
            file_map[e.caller] = _short_file(e.caller_file)
        if e.callee and e.callee_file:
            file_map[e.callee] = _short_file(e.callee_file)

    all_names: list[str] = []
    seen: set[str] = set()
    for e in graph.edges:
        for name in (e.caller, e.callee):
            if name and name.upper() not in seen:
                seen.add(name.upper())
                all_names.append(name)

    node_id: dict[str, str] = {}
    for i, name in enumerate(all_names):
        node_id[name.upper()] = f"N{i}"

    lines = [f"graph {direction}"]

    for name in all_names:
        nid = node_id[name.upper()]
        safe_name = _sanitize_mermaid(name or "")
        finfo = _sanitize_mermaid(file_map.get(name, ""))
        lbl = f"{safe_name} - {finfo}" if finfo else (safe_name or "?")
        quoted = _mermaid_quote(lbl)
        lines.append(f'    {nid}["{quoted}"]')

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

    root_key = graph.root_function.upper()
    if root_key in node_id:
        rid = node_id[root_key]
        lines.append(f"    style {rid} fill:#4CAF50,color:#fff,stroke:#388E3C")

    result = "\n".join(lines)
    logger.debug("Generated Mermaid script (%d nodes, %d edges):\n%s", len(all_names), len(graph.edges), result)
    return result


# ---------------------------------------------------------------------------
# ECharts interactive DAG
# ---------------------------------------------------------------------------

def build_echart_dag(graph: CallGraphOutput, label: str = "") -> dict:
    """Return an ECharts option dict for an interactive force-directed graph."""
    if not graph or not graph.edges:
        return {}

    file_map: dict[str, str] = {}
    for e in graph.edges:
        if e.caller and e.caller_file:
            file_map[e.caller] = _short_file(e.caller_file)
        if e.callee and e.callee_file:
            file_map[e.callee] = _short_file(e.callee_file)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_names: set[str] = set()
    edge_seen: set[tuple[str, str]] = set()

    for e in graph.edges:
        for name in (e.caller, e.callee):
            if name.upper() not in seen_names:
                seen_names.add(name.upper())
                is_root = name.upper() == graph.root_function.upper()
                finfo = file_map.get(name, "")
                tooltip = f"{name} ({finfo})" if finfo else name
                nodes.append({
                    "name": name,
                    "symbolSize": 40 if is_root else 25,
                    "category": 0 if is_root else 1,
                    "label": {"show": True, "fontSize": 11},
                    "tooltip": {"formatter": tooltip},
                    "itemStyle": {
                        "color": "#4CAF50" if is_root else "#42A5F5",
                    },
                })
        ck = (e.caller.upper(), e.callee.upper())
        if ck not in edge_seen and e.caller.upper() != e.callee.upper():
            edge_seen.add(ck)
            edges.append({"source": e.caller, "target": e.callee})

    title_text = f"{label} Call Graph" if label else "Call Graph"
    return {
        "title": {"text": title_text, "left": "center", "textStyle": {"fontSize": 14}},
        "tooltip": {"trigger": "item"},
        "series": [{
            "type": "graph",
            "layout": "force",
            "roam": True,
            "draggable": True,
            "data": nodes,
            "edges": edges,
            "edgeSymbol": ["none", "arrow"],
            "edgeSymbolSize": [0, 8],
            "force": {"repulsion": 250, "edgeLength": 140, "gravity": 0.1},
            "lineStyle": {"curveness": 0.1, "color": "#999"},
            "emphasis": {"focus": "adjacency", "lineStyle": {"width": 3}},
        }],
    }


# ---------------------------------------------------------------------------
# NetworkX + matplotlib graph rendering (kept for static PNG export)
# ---------------------------------------------------------------------------

def _find_root(graph: CallGraphOutput) -> str:
    """Determine the root node for a call graph."""
    adj = build_adjacency(graph.edges)
    if graph.root_function in adj:
        return graph.root_function
    candidates = {e.caller for e in graph.edges}
    callees = {e.callee for e in graph.edges}
    roots = candidates - callees
    return sorted(roots)[0] if roots else graph.edges[0].caller


def build_graph_png(graph: CallGraphOutput, title: str = "") -> str | None:
    """
    Render a call graph as a PNG image using NetworkX + matplotlib.
    Returns a base64-encoded PNG string, or None if rendering fails.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import networkx as nx
    except ImportError:
        logger.warning("matplotlib/networkx not available; skipping graph image")
        return None

    if not graph.edges:
        return None

    file_map: dict[str, str] = {}
    for e in graph.edges:
        if e.caller and e.caller_file:
            file_map[e.caller] = _short_file(e.caller_file)
        if e.callee and e.callee_file:
            file_map[e.callee] = _short_file(e.callee_file)

    G = nx.DiGraph()
    edge_seen: set[tuple[str, str]] = set()
    for e in graph.edges:
        key = (e.caller.upper(), e.callee.upper())
        if key in edge_seen:
            continue
        edge_seen.add(key)
        if e.caller.upper() == e.callee.upper():
            continue
        G.add_edge(e.caller, e.callee)

    if G.number_of_nodes() == 0:
        return None

    root = _find_root(graph)

    node_labels = {}
    for node in G.nodes():
        f = file_map.get(node, "")
        node_labels[node] = f"{node}\n({f})" if f else node

    try:
        pos = _hierarchical_layout(G, root)
    except Exception:
        pos = nx.spring_layout(G, seed=42, k=2.5, iterations=60)

    node_count = G.number_of_nodes()
    fig_w = max(10, min(22, node_count * 2.2))
    fig_h = max(6, min(16, node_count * 1.5))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    node_colors = []
    for node in G.nodes():
        if node.upper() == root.upper():
            node_colors.append("#4CAF50")
        else:
            node_colors.append("#42A5F5")

    node_sz = max(2400, 4000 - node_count * 60)
    nx.draw_networkx_nodes(
        G, pos, ax=ax,
        node_color=node_colors, node_size=node_sz,
        alpha=0.92, edgecolors="#333", linewidths=1.5,
    )
    nx.draw_networkx_edges(
        G, pos, ax=ax,
        edge_color="#555", arrows=True, arrowsize=20,
        arrowstyle="-|>", connectionstyle="arc3,rad=0.08",
        width=1.8, min_source_margin=18, min_target_margin=18,
    )

    label_pos = {k: (v[0], v[1] - 0.35) for k, v in pos.items()}
    nx.draw_networkx_labels(
        G, label_pos, labels=node_labels, ax=ax,
        font_size=max(7, 9 - node_count // 8),
        font_weight="bold", font_color="#222", verticalalignment="top",
    )

    short_labels = {n: n[:12] + ".." if len(n) > 14 else n for n in G.nodes()}
    nx.draw_networkx_labels(
        G, pos, labels=short_labels, ax=ax,
        font_size=max(6, 8 - node_count // 8),
        font_weight="bold", font_color="#fff",
    )

    if title:
        ax.set_title(title, fontsize=14, fontweight="bold", pad=12)

    ax.margins(0.15)
    ax.axis("off")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(
        buf, format="png", dpi=120, bbox_inches="tight",
        facecolor="#fafafa", edgecolor="none",
    )
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def _hierarchical_layout(
    G: "nx.DiGraph", root: str,
) -> dict[str, tuple[float, float]]:
    """Simple top-down hierarchical layout using BFS levels."""
    import networkx as nx

    levels: dict[str, int] = {}
    queue = [root]
    levels[root] = 0
    while queue:
        node = queue.pop(0)
        for succ in G.successors(node):
            if succ not in levels:
                levels[succ] = levels[node] + 1
                queue.append(succ)
    for node in G.nodes():
        if node not in levels:
            levels[node] = max(levels.values(), default=0) + 1

    by_level: dict[int, list[str]] = {}
    for node, lvl in levels.items():
        by_level.setdefault(lvl, []).append(node)

    pos = {}
    max_width = max(len(nodes) for nodes in by_level.values())
    for lvl, nodes in by_level.items():
        nodes.sort()
        spacing = max_width / (len(nodes) + 1)
        for i, node in enumerate(nodes):
            x = (i + 1) * spacing
            y = -lvl * 2.0
            pos[node] = (x, y)
    return pos
