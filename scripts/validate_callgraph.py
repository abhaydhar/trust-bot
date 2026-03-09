"""
Validate a YAML ground-truth call graph against the indexed codebase.

Reads the YAML exported by export_neo4j_callgraph.py, queries the SQLite
code index (code_index + call_edges tables), and produces a Markdown gap
report highlighting missing callees in either direction.

Usage:
    python scripts/validate_callgraph.py --yaml ground_truth.yaml
    python scripts/validate_callgraph.py --yaml ground_truth.yaml --db-path .trustbot_git_index.db --output report.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trustbot.config import settings


# ---------------------------------------------------------------------------
# Normalization helpers (mirrors trustbot/agents/verification.py)
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    return (name or "").strip().upper()


def _bare_name(name: str) -> str:
    """Strip leading ClassName. prefix: 'TForm1.Button2Click' -> 'BUTTON2CLICK'."""
    s = _normalize(name)
    if "." in s:
        return s.rsplit(".", 1)[-1]
    return s


def _normalize_file(path: str) -> str:
    """Normalize to uppercase filename only."""
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1].strip().upper()


# ---------------------------------------------------------------------------
# YAML flattening
# ---------------------------------------------------------------------------

def _flatten_call_tree(
    caller_key: str,
    caller_func: str,
    caller_class: str,
    caller_file: str,
    tree: list[dict],
) -> list[dict]:
    """
    Recursively flatten a nested call_tree into a flat list of edge dicts.
    Each edge: {caller_func, caller_class, caller_file, callee_func, callee_class, callee_file}.
    """
    edges = []
    for node in tree:
        edge = {
            "caller_key": caller_key,
            "caller_func": caller_func,
            "caller_class": caller_class,
            "caller_file": caller_file,
            "callee_key": node.get("callee_key", ""),
            "callee_func": node.get("callee_function", ""),
            "callee_class": node.get("callee_class", ""),
            "callee_file": node.get("callee_file", ""),
            "callee_file_path": node.get("callee_file_path", ""),
            "depth": node.get("depth", 1),
        }
        edges.append(edge)
        edges.extend(_flatten_call_tree(
            node.get("callee_key", ""),
            node.get("callee_function", ""),
            node.get("callee_class", ""),
            node.get("callee_file", ""),
            node.get("callees", []),
        ))
    return edges


# ---------------------------------------------------------------------------
# Codebase index querying
# ---------------------------------------------------------------------------

class CodebaseQuerier:
    """Query the SQLite code index for functions and call edges."""

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._functions: dict[str, list[dict]] | None = None
        self._edges: dict[str, list[dict]] | None = None

    def _load_functions(self):
        if self._functions is not None:
            return
        self._functions = defaultdict(list)
        rows = self._conn.execute(
            "SELECT function_name, file_path, class_name FROM code_index"
        ).fetchall()
        for r in rows:
            key = _normalize(r["function_name"])
            self._functions[key].append({
                "function_name": r["function_name"],
                "file_path": r["file_path"],
                "class_name": r["class_name"] or "",
            })

    def _load_edges(self):
        if self._edges is not None:
            return
        self._edges = defaultdict(list)
        rows = self._conn.execute(
            "SELECT caller, callee, confidence FROM call_edges"
        ).fetchall()
        for r in rows:
            caller_parts = r["caller"].replace("\\", "/").split("::")
            callee_parts = r["callee"].replace("\\", "/").split("::")
            caller_func = caller_parts[-1] if caller_parts else r["caller"]
            callee_func = callee_parts[-1] if callee_parts else r["callee"]
            caller_class = caller_parts[1] if len(caller_parts) >= 3 else ""
            callee_class = callee_parts[1] if len(callee_parts) >= 3 else ""
            caller_file = caller_parts[0].rsplit("/", 1)[-1] if caller_parts else ""
            callee_file = callee_parts[0].rsplit("/", 1)[-1] if callee_parts else ""

            norm_caller = _normalize(caller_func)
            self._edges[norm_caller].append({
                "caller_func": caller_func,
                "caller_class": caller_class,
                "caller_file": caller_file,
                "callee_func": callee_func,
                "callee_class": callee_class,
                "callee_file": callee_file,
                "confidence": r["confidence"],
                "raw_caller": r["caller"],
                "raw_callee": r["callee"],
            })

    def function_exists(self, name: str) -> bool:
        self._load_functions()
        return _normalize(name) in self._functions or _bare_name(name) in self._functions

    def get_callees_for(self, caller_func: str) -> list[dict]:
        """Return all codebase callees for a given caller function name."""
        self._load_edges()
        results = self._edges.get(_normalize(caller_func), [])
        if not results:
            results = self._edges.get(_bare_name(caller_func), [])
        return results

    def get_all_edges_flat(self) -> set[tuple[str, str]]:
        """Return all (caller, callee) pairs normalized."""
        self._load_edges()
        pairs = set()
        for caller_key, edge_list in self._edges.items():
            for e in edge_list:
                pairs.add((_normalize(e["caller_func"]), _normalize(e["callee_func"])))
        return pairs

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def _match_edge(yaml_edge: dict, codebase: CodebaseQuerier) -> dict:
    """
    Try to match a YAML edge against the codebase using 3-tier matching.
    Returns a result dict with 'status' and 'match_tier'.
    """
    callee_func = yaml_edge["callee_func"]
    callee_class = yaml_edge["callee_class"]
    callee_file = yaml_edge["callee_file"]
    caller_func = yaml_edge["caller_func"]

    cb_callees = codebase.get_callees_for(caller_func)

    norm_callee = _normalize(callee_func)
    bare_callee = _bare_name(callee_func)
    norm_callee_file = _normalize_file(callee_file)
    norm_callee_class = _normalize(callee_class)

    # Tier 1: full match (function + class + file)
    for cb in cb_callees:
        if (_normalize(cb["callee_func"]) == norm_callee or _bare_name(cb["callee_func"]) == bare_callee):
            if _normalize_file(cb["callee_file"]) == norm_callee_file and norm_callee_file:
                if _normalize(cb["callee_class"]) == norm_callee_class and norm_callee_class:
                    return {"status": "confirmed", "match_tier": "full_match", "detail": "name + class + file"}

    # Tier 2: name + file match (ignore class)
    for cb in cb_callees:
        if (_normalize(cb["callee_func"]) == norm_callee or _bare_name(cb["callee_func"]) == bare_callee):
            if _normalize_file(cb["callee_file"]) == norm_callee_file and norm_callee_file:
                return {"status": "confirmed", "match_tier": "name_file_match", "detail": "name + file (class ignored)"}

    # Tier 3: name-only match
    for cb in cb_callees:
        if (_normalize(cb["callee_func"]) == norm_callee or _bare_name(cb["callee_func"]) == bare_callee):
            return {"status": "confirmed", "match_tier": "name_only_match", "detail": "name only"}

    # Check if the callee function exists anywhere in the codebase index
    if codebase.function_exists(callee_func):
        return {"status": "missing_edge", "match_tier": "none",
                "detail": f"Function '{callee_func}' exists in codebase but no call edge from '{caller_func}'"}

    return {"status": "missing_function", "match_tier": "none",
            "detail": f"Function '{callee_func}' not found in codebase index"}


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _generate_report(
    yaml_data: dict,
    snippet_results: list[dict],
) -> str:
    """Generate the markdown validation report."""
    pid = yaml_data["project_id"]
    rid = yaml_data["run_id"]

    total_yaml_edges = sum(sr["total_callees"] for sr in snippet_results)
    total_confirmed = sum(sr["confirmed_count"] for sr in snippet_results)
    total_missing_edge = sum(1 for sr in snippet_results for me in sr["missing_edges"] if me["status"] == "missing_edge")
    total_missing_func = sum(1 for sr in snippet_results for me in sr["missing_edges"] if me["status"] == "missing_function")
    total_extra = sum(len(sr["extra_edges"]) for sr in snippet_results)
    total_full = sum(sr["match_counts"]["full_match"] for sr in snippet_results)
    total_name_file = sum(sr["match_counts"]["name_file_match"] for sr in snippet_results)
    total_name_only = sum(sr["match_counts"]["name_only_match"] for sr in snippet_results)

    snippets_with_gaps = [sr for sr in snippet_results if sr["missing_edges"] or sr["extra_edges"]]
    snippets_ok = [sr for sr in snippet_results if sr["total_callees"] > 0 and not sr["missing_edges"]]

    lines = [
        f"# Call Graph Validation Report",
        f"",
        f"**Project ID**: {pid} | **Run ID**: {rid}  ",
        f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Source YAML**: {yaml_data.get('exported_at', 'N/A')}",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Count |",
        f"|---|---:|",
        f"| Total Snippets in YAML | {yaml_data.get('total_snippets', 'N/A')} |",
        f"| Snippets with outgoing calls | {sum(1 for sr in snippet_results if sr['total_callees'] > 0)} |",
        f"| Total YAML call edges | {total_yaml_edges} |",
        f"| Confirmed in codebase | {total_confirmed} |",
        f"| &nbsp;&nbsp;Full match (name+class+file) | {total_full} |",
        f"| &nbsp;&nbsp;Name+file match | {total_name_file} |",
        f"| &nbsp;&nbsp;Name-only match | {total_name_only} |",
        f"| Missing edge (function exists, no call edge) | {total_missing_edge} |",
        f"| Missing function (not in codebase at all) | {total_missing_func} |",
        f"| Extra in codebase (not in YAML) | {total_extra} |",
        f"",
    ]

    if total_yaml_edges > 0:
        pct = total_confirmed / total_yaml_edges * 100
        lines.append(f"**Coverage**: {pct:.1f}% of YAML edges confirmed in codebase")
        lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## Per-Snippet Detail",
        f"",
    ])

    for sr in snippet_results:
        s_func = sr["function_name"] or sr["key"]
        s_class = sr["class_name"]
        s_file = sr["file_name"]
        total = sr["total_callees"]
        confirmed = sr["confirmed_count"]

        if total == 0 and not sr["extra_edges"]:
            continue

        label = f"{s_func}"
        if s_class:
            label += f" ({s_class})"
        if s_file:
            label += f" — {s_file}"

        has_gaps = bool(sr["missing_edges"])
        status_icon = "GAPS" if has_gaps else "OK"
        lines.append(f"### {label}")
        lines.append(f"**Status**: {confirmed}/{total} callees confirmed {'**' + status_icon + '**' if has_gaps else status_icon}")
        lines.append("")

        if sr["missing_edges"]:
            lines.append("**Missing from codebase:**")
            lines.append("")
            for me in sr["missing_edges"]:
                lines.append(f"- `{me['callee_func']}` ({me['callee_class']}, {me['callee_file']}) "
                             f"— {me['detail']}")
            lines.append("")

        if sr["extra_edges"]:
            lines.append("**Extra in codebase (not in YAML):**")
            lines.append("")
            for ee in sr["extra_edges"]:
                lines.append(f"- `{ee['callee_func']}` ({ee['callee_class']}, {ee['callee_file']})")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Validate YAML ground-truth call graph against codebase index",
    )
    parser.add_argument("--yaml", type=str, required=True, help="Path to ground_truth.yaml")
    parser.add_argument("--db-path", type=str, default=None,
                        help="Path to .trustbot_git_index.db (default: codebase_root/.trustbot_git_index.db)")
    parser.add_argument("--output", type=str, default="validation_report.md")
    args = parser.parse_args()

    yaml_path = Path(args.yaml)
    if not yaml_path.exists():
        print(f"ERROR: YAML file not found: {yaml_path}")
        sys.exit(1)

    print(f"Loading YAML ground truth from {yaml_path} ...")
    with open(yaml_path, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f)

    pid = yaml_data["project_id"]
    rid = yaml_data["run_id"]
    snippets = yaml_data.get("snippets", [])
    print(f"  Project={pid}, Run={rid}, Snippets={len(snippets)}")

    db_path = Path(args.db_path) if args.db_path else (settings.codebase_root / ".trustbot_git_index.db")
    if not db_path.exists():
        print(f"ERROR: Code index DB not found: {db_path}")
        sys.exit(1)

    print(f"Opening codebase index: {db_path}")
    codebase = CodebaseQuerier(db_path)

    all_codebase_edges = codebase.get_all_edges_flat()
    print(f"  Codebase has {len(all_codebase_edges)} unique call edges.\n")

    snippet_results = []

    for snippet in snippets:
        s_key = snippet.get("key", "")
        s_func = snippet.get("function_name", "")
        s_class = snippet.get("class_name", "")
        s_file = snippet.get("file_name", "")
        call_tree = snippet.get("call_tree", [])

        flat_edges = _flatten_call_tree(s_key, s_func, s_class, s_file, call_tree)
        seen_callees: set[str] = set()
        unique_edges = []
        for e in flat_edges:
            callee_norm = _normalize(e["callee_func"])
            if callee_norm not in seen_callees:
                seen_callees.add(callee_norm)
                unique_edges.append(e)

        confirmed_count = 0
        missing_edges = []
        match_counts = {"full_match": 0, "name_file_match": 0, "name_only_match": 0}

        for edge in unique_edges:
            result = _match_edge(edge, codebase)
            if result["status"] == "confirmed":
                confirmed_count += 1
                match_counts[result["match_tier"]] += 1
            else:
                missing_edges.append({
                    "callee_func": edge["callee_func"],
                    "callee_class": edge["callee_class"],
                    "callee_file": edge["callee_file"],
                    "detail": result["detail"],
                    "status": result["status"],
                })

        cb_callees = codebase.get_callees_for(s_func)
        extra_edges = []
        for cb in cb_callees:
            cb_callee_norm = _normalize(cb["callee_func"])
            if cb_callee_norm not in seen_callees and _bare_name(cb["callee_func"]) not in seen_callees:
                extra_edges.append(cb)

        snippet_results.append({
            "key": s_key,
            "function_name": s_func,
            "class_name": s_class,
            "file_name": s_file,
            "total_callees": len(unique_edges),
            "confirmed_count": confirmed_count,
            "missing_edges": missing_edges,
            "extra_edges": extra_edges,
            "match_counts": match_counts,
        })

        if unique_edges or extra_edges:
            print(f"  {s_func or s_key}: {confirmed_count}/{len(unique_edges)} confirmed"
                  f"{f', {len(missing_edges)} missing' if missing_edges else ''}"
                  f"{f', {len(extra_edges)} extra' if extra_edges else ''}")

    print(f"\nGenerating report ...")
    report = _generate_report(yaml_data, snippet_results)
    codebase.close()

    out_path = Path(args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Report written to {out_path.resolve()}")

    total_yaml = sum(sr["total_callees"] for sr in snippet_results)
    total_ok = sum(sr["confirmed_count"] for sr in snippet_results)
    if total_yaml > 0:
        print(f"\nOverall coverage: {total_ok}/{total_yaml} ({total_ok/total_yaml*100:.1f}%)")


if __name__ == "__main__":
    main()
