"""
Export Neo4j call graphs to a YAML ground-truth file.

Connects to Neo4j, fetches all Snippet nodes and CALLS edges for a given
project_id / run_id, builds recursive call trees per snippet, and writes
the result as a structured YAML file.

Usage:
    python scripts/export_neo4j_callgraph.py --project-id 3191 --run-id 4970
    python scripts/export_neo4j_callgraph.py --project-id 3191 --run-id 4970 --output my_ground_truth.yaml
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from neo4j import GraphDatabase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trustbot.config import settings


def _connect():
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    driver.verify_connectivity()
    return driver


def _fetch_snippets(driver, pid: int, rid: int) -> dict[str, dict]:
    """Fetch all Snippet nodes for the project/run, keyed by Snippet key."""
    query = """
    MATCH (s:Snippet {project_id: $pid, run_id: $rid})
    RETURN s
    """
    snippets: dict[str, dict] = {}
    with driver.session() as session:
        result = session.run(query, pid=pid, rid=rid)
        for record in result:
            node = dict(record["s"])
            key = node.get("key", "")
            snippets[key] = node
    return snippets


def _fetch_calls_edges(driver, pid: int, rid: int) -> list[dict]:
    """Fetch all CALLS relationships between Snippets for the project/run."""
    query = """
    MATCH (caller:Snippet {project_id: $pid, run_id: $rid})-[c:CALLS]->(callee:Snippet)
    RETURN caller.key AS caller_key,
           callee.key AS callee_key,
           callee.function_name AS callee_function,
           callee.class_name AS callee_class,
           callee.file_name AS callee_file_name,
           callee.file_path AS callee_file_path,
           properties(c) AS call_props
    """
    edges = []
    with driver.session() as session:
        result = session.run(query, pid=pid, rid=rid)
        for record in result:
            edges.append({
                "caller_key": record["caller_key"],
                "callee_key": record["callee_key"],
                "callee_function": record["callee_function"] or "",
                "callee_class": record["callee_class"] or "",
                "callee_file_name": record["callee_file_name"] or "",
                "callee_file_path": record["callee_file_path"] or "",
                "execution_order": (record["call_props"] or {}).get("execution_order", 0),
            })
    return edges


def _build_adjacency(edges: list[dict]) -> dict[str, list[dict]]:
    """Build caller_key -> [edge, ...] adjacency map, sorted by execution_order."""
    adj: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        adj[e["caller_key"]].append(e)
    for key in adj:
        adj[key].sort(key=lambda x: x.get("execution_order", 0))
    return adj


def _build_call_tree(
    root_key: str,
    adj: dict[str, list[dict]],
    snippets: dict[str, dict],
    depth: int = 1,
    visited: set | None = None,
) -> list[dict]:
    """Recursively build a call tree from root_key using the adjacency map."""
    if visited is None:
        visited = set()

    if root_key in visited:
        return []
    visited.add(root_key)

    children = []
    for edge in adj.get(root_key, []):
        callee_key = edge["callee_key"]
        callee_node = snippets.get(callee_key, {})
        child = {
            "callee_key": callee_key,
            "callee_function": edge["callee_function"] or callee_node.get("function_name", ""),
            "callee_class": edge["callee_class"] or callee_node.get("class_name", ""),
            "callee_file": edge["callee_file_name"] or callee_node.get("file_name", ""),
            "callee_file_path": edge["callee_file_path"] or callee_node.get("file_path", ""),
            "depth": depth,
            "execution_order": edge.get("execution_order", 0),
            "callees": _build_call_tree(
                callee_key, adj, snippets, depth + 1, visited.copy(),
            ),
        }
        children.append(child)
    return children


def main():
    parser = argparse.ArgumentParser(
        description="Export Neo4j call graphs to YAML ground-truth file",
    )
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output", type=str, default="ground_truth.yaml")
    args = parser.parse_args()

    pid, rid = args.project_id, args.run_id
    print(f"Connecting to Neo4j at {settings.neo4j_uri} ...")
    driver = _connect()
    print("Connected.\n")

    print(f"Fetching all Snippets for project={pid}, run={rid} ...")
    all_snippets = _fetch_snippets(driver, pid, rid)
    print(f"  Found {len(all_snippets)} snippet(s).\n")

    print("Fetching all CALLS edges ...")
    all_edges = _fetch_calls_edges(driver, pid, rid)
    print(f"  Found {len(all_edges)} edge(s).\n")

    adj = _build_adjacency(all_edges)

    # Identify which snippets have outgoing calls (callers)
    callers = set(adj.keys())
    # Identify which snippets are callees
    callees_set = {e["callee_key"] for e in all_edges}

    snippet_entries = []
    for s_key, s_node in all_snippets.items():
        has_callees = s_key in callers
        is_callee = s_key in callees_set
        call_tree = _build_call_tree(s_key, adj, all_snippets) if has_callees else []

        snippet_entries.append({
            "key": s_key,
            "function_name": s_node.get("function_name", ""),
            "class_name": s_node.get("class_name", ""),
            "file_path": s_node.get("file_path", ""),
            "file_name": s_node.get("file_name", ""),
            "type": s_node.get("type", ""),
            "is_caller": has_callees,
            "is_callee": is_callee,
            "call_tree": call_tree,
        })

    driver.close()

    print(f"Building YAML ...")
    callers_count = sum(1 for s in snippet_entries if s["is_caller"])
    leaf_count = sum(1 for s in snippet_entries if not s["is_caller"] and s["is_callee"])
    print(f"  {callers_count} snippets with outgoing calls, {leaf_count} leaf callees")

    output = {
        "project_id": pid,
        "run_id": rid,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_snippets": len(all_snippets),
        "total_edges": len(all_edges),
        "snippets": snippet_entries,
    }

    out_path = Path(args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(output, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"\nExported {len(all_snippets)} snippets and {len(all_edges)} edges "
          f"to {out_path.resolve()}")


if __name__ == "__main__":
    main()
