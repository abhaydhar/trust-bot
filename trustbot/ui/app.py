"""NiceGUI-based web UI for TrustBot with 3-agent validation pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

from nicegui import ui

from trustbot.config import settings
from trustbot.models.agentic import VerificationResult

logger = logging.getLogger("trustbot.ui")

# ---------------------------------------------------------------------------
# Lazy access to the registry (initialised at startup in main.py)
# ---------------------------------------------------------------------------


def _get_registry():
    from trustbot.main import get_registry
    return get_registry()


def _short_chunk_id(chunk_id: str) -> str:
    """Convert 'path/file.pas::ClassName::FuncName' to 'FuncName (file.pas)'."""
    parts = chunk_id.split("::")
    if len(parts) >= 3:
        file_part = parts[0].replace("\\", "/").rsplit("/", 1)[-1]
        func_name = parts[2] or parts[1] or file_part
        return f"{func_name} ({file_part})"
    if len(parts) == 2:
        file_part = parts[0].replace("\\", "/").rsplit("/", 1)[-1]
        return f"{parts[1]} ({file_part})"
    return chunk_id


# ---------------------------------------------------------------------------
# Shared mutable state (module-level, set during UI callbacks)
# ---------------------------------------------------------------------------

_git_index = None
_pipeline = None
_orchestrator = None

_progress_state = {"step": "", "pct": 0.0, "done": False}
_progress_lock = threading.Lock()


def _set_progress(pct: float, step: str, done: bool = False):
    with _progress_lock:
        _progress_state["pct"] = pct
        _progress_state["step"] = step
        _progress_state["done"] = done


def _get_progress():
    with _progress_lock:
        return dict(_progress_state)


# ---------------------------------------------------------------------------
# Async backend handlers
# ---------------------------------------------------------------------------


async def _clone_and_index_repo(git_url: str, branch: str, progress_cb=None):
    """Clone a git repo and build code index."""
    global _git_index
    if not git_url.strip():
        return "Please enter a Git repository URL."
    try:
        if progress_cb:
            progress_cb(0, "Cloning repository...")
        from trustbot.indexing.git_indexer import GitCodeIndexer
        indexer = GitCodeIndexer()
        if progress_cb:
            progress_cb(0.2, "Downloading code...")

        result = await indexer.clone_and_index(
            git_url.strip(), branch.strip() or "main",
            progress_callback=lambda p, d: progress_cb(0.2 + 0.6 * p, d) if progress_cb else None,
        )
        if progress_cb:
            progress_cb(0.9, "Finalizing...")

        from trustbot.index.code_index import CodeIndex
        git_index_path = settings.codebase_root / ".trustbot_git_index.db"
        _git_index = CodeIndex(db_path=git_index_path)

        if _pipeline:
            _pipeline.set_code_index(_git_index)

        if progress_cb:
            progress_cb(1.0, "Done!")
        return (
            f"## Indexing Complete!\n\n"
            f"**Repository**: {git_url}\n"
            f"**Branch**: {branch or 'main'}\n"
            f"**Files processed**: {result['files']}\n"
            f"**Code chunks created**: {result['chunks']}\n"
            f"**Functions indexed**: {result['functions']}\n"
            f"**Call graph edges**: {result['edges']}\n"
            f"**Duration**: {result['duration']:.1f}s\n\n"
            f"Codebase is ready. Switch to the **Validate** tab to start validation."
        )
    except ImportError:
        return "Error: GitPython not installed. Run: pip install gitpython"
    except Exception as e:
        logger.exception("Git indexing failed")
        return f"Error: {e}"


async def _index_local_folder(folder_path: str, progress_cb=None):
    """Index code directly from a local folder."""
    global _git_index
    folder_path = (folder_path or "").strip()
    if not folder_path:
        return "Please enter a folder path."

    folder = Path(folder_path)
    if not folder.exists():
        return f"Folder does not exist: `{folder}`"
    if not folder.is_dir():
        return f"Path is not a directory: `{folder}`"

    try:
        if progress_cb:
            progress_cb(0.05, "Scanning local folder...")

        from trustbot.indexing.chunker import chunk_codebase
        from trustbot.indexing.call_graph_builder import build_call_graph_from_chunks

        chunks = await asyncio.to_thread(chunk_codebase, folder)

        if progress_cb:
            progress_cb(0.35, f"Found {len(chunks)} code chunks, building index...")

        from trustbot.index.code_index import CodeIndex
        git_index_path = settings.codebase_root / ".trustbot_git_index.db"
        code_idx = CodeIndex(db_path=git_index_path)
        code_idx.build(codebase_root=folder)

        function_count = len([c for c in chunks if c.function_name])
        if progress_cb:
            progress_cb(0.55, f"Building call graph from {function_count} functions...")

        edges = await asyncio.to_thread(build_call_graph_from_chunks, chunks)

        edge_tuples = [(e.from_chunk, e.to_chunk, e.confidence) for e in edges]
        code_idx.store_edges(edge_tuples)
        code_idx.close()

        if progress_cb:
            progress_cb(0.9, "Finalizing...")

        _git_index = CodeIndex(db_path=git_index_path)
        if _pipeline:
            _pipeline.set_code_index(_git_index)

        files_count = len({c.file_path for c in chunks})
        if progress_cb:
            progress_cb(1.0, "Done!")

        return (
            f"## Indexing Complete!\n\n"
            f"**Source**: Local Folder\n"
            f"**Path**: `{folder}`\n"
            f"**Files processed**: {files_count}\n"
            f"**Code chunks created**: {len(chunks)}\n"
            f"**Functions indexed**: {function_count}\n"
            f"**Call graph edges**: {len(edges)}\n\n"
            f"Codebase is ready. Switch to the **Validate** tab to start validation."
        )
    except Exception as e:
        logger.exception("Local folder indexing failed")
        return f"Error: {e}"


async def _validate_all_flows(project_id: int, run_id: int):
    """3-agent validation across all flows in a project."""
    if not _pipeline:
        return None, "Pipeline not available. Neo4j tool is missing."
    if not _pipeline.has_index:
        return None, (
            "**No codebase indexed.** Please go to the **Code Indexer** tab first, "
            "clone the repository, and then return here to validate."
        )

    try:
        registry = _get_registry()
        _set_progress(0.02, "Connecting to Neo4j...")
        neo4j_tool = registry.get("neo4j")

        _set_progress(0.06, "Fetching execution flows...")
        flows = await neo4j_tool.call(
            "get_execution_flows_by_project",
            project_id=project_id, run_id=run_id,
        )
        total_flows = len(flows)
        _set_progress(0.10, f"Found {total_flows} flows to validate...")

        all_results: list[dict] = []
        for idx, flow in enumerate(flows):
            base_pct = 0.10 + 0.80 * (idx / total_flows)
            step_width = 0.80 / total_flows
            flow_name = flow.name or flow.key

            def _agent_progress(agent, msg, _bp=base_pct, _sw=step_width):
                offsets = {"agent1": 0.0, "agent2": 0.33, "agent3": 0.66}
                labels = {"agent1": "Agent 1", "agent2": "Agent 2", "agent3": "Agent 3"}
                pct = _bp + _sw * offsets.get(agent, 0.0)
                label = labels.get(agent, agent)
                _set_progress(pct, f"Flow {idx+1}/{total_flows} -- {label}: {msg}")

            result, report_md, neo4j_g, index_g = await _pipeline.validate_flow(
                flow.key, progress_callback=_agent_progress,
            )
            all_results.append({
                "flow_key": flow.key,
                "flow_name": flow_name,
                "result": result,
                "report_md": report_md,
                "neo4j_edges": len(neo4j_g.edges),
                "index_edges": len(index_g.edges),
                "neo4j_graph": neo4j_g,
                "index_graph": index_g,
            })

        _set_progress(0.92, "Generating report...")
        _set_progress(1.0, "Validation complete!", done=True)
        return all_results, None

    except Exception as e:
        logger.exception("Validation failed")
        _set_progress(1.0, f"Error: {e}", done=True)
        return None, f"Unexpected error: {e}"


# ---------------------------------------------------------------------------
# Report formatting helpers (pure-string, no UI dependency)
# ---------------------------------------------------------------------------


def _format_3agent_summary(project_id: int, run_id: int, results: list[dict]) -> str:
    total_confirmed = sum(len(r["result"].confirmed_edges) for r in results)
    total_phantom = sum(len(r["result"].phantom_edges) for r in results)
    total_missing = sum(len(r["result"].missing_edges) for r in results)
    total_edges = total_confirmed + total_phantom + total_missing

    avg_trust = 0.0
    if results:
        avg_trust = sum(r["result"].flow_trust_score for r in results) / len(results)

    needs_attention = [
        r for r in results
        if r["result"].phantom_edges or r["result"].missing_edges
    ]

    lines = [
        "## 3-Agent Validation Summary",
        f"**Project ID**: {project_id} | **Run ID**: {run_id} | **Flows**: {len(results)}",
        "",
        "### Key Metrics",
        f"- **Average Trust Score**: {avg_trust:.0%}",
        f"- **Total Edges Analyzed**: {total_edges}",
        f"  - Confirmed: {total_confirmed}",
        f"  - Phantom (Neo4j only): {total_phantom}",
        f"  - Missing (Index only): {total_missing}",
        "",
    ]

    if needs_attention:
        lines.append(f"### Flows Requiring Attention ({len(needs_attention)})")
        from trustbot.agents.flow_attention import analyze_flow_attention
        for r in needs_attention[:10]:
            res = r["result"]
            lines.append(
                f"- **{r['flow_name']}** (`{r['flow_key'][:12]}...`): "
                f"trust {res.flow_trust_score:.0%}, "
                f"{len(res.phantom_edges)} phantom, {len(res.missing_edges)} missing"
            )
            analysis = analyze_flow_attention(
                result=res,
                neo4j_graph=r.get("neo4j_graph"),
                index_graph=r.get("index_graph"),
            )
            causes = analysis.get("likely_causes") or []
            if causes:
                lines.append(f"  - *Likely causes:* {'; '.join(causes[:3])}")
        lines.append("")

    return "\n".join(lines)


def _format_3agent_report(project_id: int, run_id: int, results: list[dict]) -> str:
    from trustbot.models.agentic import CallGraphOutput, normalize_file_path
    from trustbot.ui.call_tree import build_text_tree

    lines = [
        "# 3-Agent Validation Report",
        f"**Project ID**: {project_id} | **Run ID**: {run_id} | "
        f"**Flows validated**: {len(results)}",
        "",
    ]

    for idx, r in enumerate(results):
        res: VerificationResult = r["result"]
        flow_name = r["flow_name"]
        flow_key = r["flow_key"]
        neo4j_edges = r["neo4j_edges"]
        index_edges = r["index_edges"]
        meta = res.metadata
        neo4j_graph: CallGraphOutput | None = r.get("neo4j_graph")
        index_graph: CallGraphOutput | None = r.get("index_graph")

        lines.append("---")
        lines.append(f"## Flow {idx+1}/{len(results)}: {flow_name}")
        lines.append(
            f"**Key**: `{flow_key}` | "
            f"**Trust**: {res.flow_trust_score:.0%} | "
            f"**Neo4j edges**: {neo4j_edges} | **Index edges**: {index_edges}"
        )
        lines.append("")

        lines.append("### Agent 1 — Neo4j Call Graph")
        lines.append("")
        if neo4j_graph:
            lines.append(
                f"**Root**: `{neo4j_graph.root_function}` | "
                f"**Edges**: {len(neo4j_graph.edges)}"
            )
            lines.append("")
            if neo4j_graph.edges:
                tree = build_text_tree(neo4j_graph, "Neo4j")
                lines += ["**Call Tree:**", "", "```", tree, "```", ""]
                lines += ["**Edge Details:**", ""]
                lines.append("| # | Caller | Class | File | Callee | Class | File |")
                lines.append("|---|--------|-------|------|--------|-------|------|")
                for i, e in enumerate(neo4j_graph.edges[:40], 1):
                    cr_file = normalize_file_path(e.caller_file) or "-"
                    ce_file = normalize_file_path(e.callee_file) or "-"
                    lines.append(
                        f"| {i} | `{e.caller}` | {e.caller_class or '-'} | {cr_file} "
                        f"| `{e.callee}` | {e.callee_class or '-'} | {ce_file} |"
                    )
                if len(neo4j_graph.edges) > 40:
                    lines.append(
                        f"| ... | +{len(neo4j_graph.edges) - 40} more | | | | | |"
                    )
            else:
                lines.append("*No edges.*")
            lines.append("")
        else:
            lines.append("*Agent 1 data not available.*")
            lines.append("")

        lines.append("### Agent 2 — Indexed Codebase Call Graph")
        lines.append("")
        if index_graph:
            lines.append(
                f"**Root**: `{index_graph.root_function}` | "
                f"**Edges**: {len(index_graph.edges)}"
            )
            idx_meta = index_graph.metadata
            if idx_meta.get("resolved_via") and idx_meta["resolved_via"] != "original":
                lines.append(
                    f"**Resolved via**: {idx_meta['resolved_via']} "
                    f"(original root: `{idx_meta.get('original_root', '')}`)"
                )
            if idx_meta.get("root_found_in_index") is not None:
                lines.append(
                    f"**Root in index**: {idx_meta.get('root_found_in_index')} | "
                    f"**Root has outgoing edges**: {idx_meta.get('root_has_outgoing_edges')} | "
                    f"**Index functions**: {idx_meta.get('index_functions', '-')} | "
                    f"**Index edges**: {idx_meta.get('index_edges', '-')}"
                )
            lines.append("")
            if index_graph.edges:
                tree = build_text_tree(index_graph, "Index")
                lines += ["**Call Tree:**", "", "```", tree, "```", ""]
                lines += ["**Edge Details:**", ""]
                lines.append(
                    "| # | Caller | Class | File | Callee | Class | File | Conf |"
                )
                lines.append(
                    "|---|--------|-------|------|--------|-------|------|------|"
                )
                for i, e in enumerate(index_graph.edges[:40], 1):
                    cr_file = normalize_file_path(e.caller_file) or "-"
                    ce_file = normalize_file_path(e.callee_file) or "-"
                    lines.append(
                        f"| {i} | `{e.caller}` | {e.caller_class or '-'} | {cr_file} "
                        f"| `{e.callee}` | {e.callee_class or '-'} | {ce_file} "
                        f"| {e.confidence:.2f} |"
                    )
                if len(index_graph.edges) > 40:
                    lines.append(
                        f"| ... | +{len(index_graph.edges) - 40} more | | | | | | |"
                    )
            else:
                lines.append(
                    "**No edges found.** The root function may not have outgoing calls "
                    "in the indexed codebase."
                )
            lines.append("")
            if index_graph.unresolved_callees:
                lines.append(
                    f"**Unresolved callees** ({len(index_graph.unresolved_callees)}): "
                    + ", ".join(
                        f"`{u}`" for u in index_graph.unresolved_callees[:15]
                    )
                )
                lines.append("")
        else:
            lines.append("*Agent 2 data not available.*")
            lines.append("")

        lines.append("### Agent 3 — Comparison Results")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Trust Score | {res.flow_trust_score:.2%} |")
        lines.append(f"| Confirmed | {len(res.confirmed_edges)} |")
        lines.append(
            f"| -- Full match (name+class+file) | {meta.get('match_full', '-')} |"
        )
        lines.append(f"| -- Name+file match | {meta.get('match_name_file', '-')} |")
        lines.append(f"| -- Name-only match | {meta.get('match_name_only', '-')} |")
        lines.append(f"| Phantom (Neo4j only) | {len(res.phantom_edges)} |")
        lines.append(f"| Missing (Index only) | {len(res.missing_edges)} |")
        lines.append("")

        if res.confirmed_edges:
            lines += ["**Confirmed Edges**", ""]
            lines.append("| # | Caller | Callee | Trust | Match Type |")
            lines.append("|---|--------|--------|-------|------------|")
            for i, e in enumerate(res.confirmed_edges[:30], 1):
                lines.append(
                    f"| {i} | `{e.caller}` | `{e.callee}` "
                    f"| {e.trust_score:.2f} | {e.details} |"
                )
            if len(res.confirmed_edges) > 30:
                lines.append(
                    f"| ... | +{len(res.confirmed_edges) - 30} more | | | |"
                )
            lines.append("")

        if res.phantom_edges:
            lines += ["**Phantom Edges** (in Neo4j but NOT in indexed codebase)", ""]
            lines.append("| # | Caller | Callee | Details |")
            lines.append("|---|--------|--------|---------|")
            for i, e in enumerate(res.phantom_edges[:30], 1):
                lines.append(
                    f"| {i} | `{e.caller}` | `{e.callee}` | {e.details} |"
                )
            if len(res.phantom_edges) > 30:
                lines.append(
                    f"| ... | +{len(res.phantom_edges) - 30} more | | |"
                )
            lines.append("")

        if res.missing_edges:
            lines += ["**Missing Edges** (in indexed codebase but NOT in Neo4j)", ""]
            lines.append("| # | Caller | Callee | Details |")
            lines.append("|---|--------|--------|---------|")
            for i, e in enumerate(res.missing_edges[:30], 1):
                lines.append(
                    f"| {i} | `{e.caller}` | `{e.callee}` | {e.details} |"
                )
            if len(res.missing_edges) > 30:
                lines.append(
                    f"| ... | +{len(res.missing_edges) - 30} more | | |"
                )
            lines.append("")

        if res.unresolved_callees:
            lines.append(
                f"**Unresolved Callees** ({len(res.unresolved_callees)})"
            )
            lines.append("")
            for u in res.unresolved_callees[:20]:
                lines.append(f"- `{u}`")
            lines.append("")

        # Deeper analysis for flows requiring attention (why edges don't match, fix suggestions)
        if res.phantom_edges or res.missing_edges:
            from trustbot.agents.flow_attention import (
                analyze_flow_attention,
                format_flow_attention_markdown,
            )
            analysis = analyze_flow_attention(
                result=res,
                neo4j_graph=neo4j_graph,
                index_graph=index_graph,
            )
            lines.append("### Why this flow doesn't match — analysis")
            lines.append("")
            lines.append(
                format_flow_attention_markdown(analysis, flow_name=flow_name)
            )
            lines.append("")

    return "\n".join(lines)


def _format_agent_output(title: str, results: list[dict], graph_key: str) -> str:
    from trustbot.models.agentic import CallGraphOutput, normalize_file_path

    lines = [f"# {title}", ""]

    for idx, r in enumerate(results):
        graph: CallGraphOutput = r.get(graph_key)
        if not graph:
            continue

        flow_name = r["flow_name"]
        flow_key = r["flow_key"]

        lines.append("---")
        lines.append(f"## Flow {idx+1}: {flow_name}")
        lines.append(
            f"**Key**: `{flow_key}` | "
            f"**Root**: `{graph.root_function}` | "
            f"**Source**: {graph.source.value} | "
            f"**Edges**: {len(graph.edges)}"
        )
        lines.append("")

        meta = graph.metadata
        if meta:
            meta_items = []
            for k, v in meta.items():
                if k != "validation_timestamp":
                    meta_items.append(f"**{k}**: {v}")
            if meta_items:
                lines.append(" | ".join(meta_items))
                lines.append("")

        if graph.edges:
            lines.append(
                "| # | Caller | Class | File | Callee | Class | File | Conf |"
            )
            lines.append(
                "|---|--------|-------|------|--------|-------|------|------|"
            )
            for i, e in enumerate(graph.edges[:50], 1):
                cr_file = normalize_file_path(e.caller_file) if e.caller_file else "-"
                ce_file = normalize_file_path(e.callee_file) if e.callee_file else "-"
                cr_cls = e.caller_class or "-"
                ce_cls = e.callee_class or "-"
                lines.append(
                    f"| {i} | `{e.caller}` | {cr_cls} | {cr_file} "
                    f"| `{e.callee}` | {ce_cls} | {ce_file} "
                    f"| {e.confidence:.2f} |"
                )
            if len(graph.edges) > 50:
                lines.append(
                    f"| ... | +{len(graph.edges) - 50} more | | | | | | |"
                )
            lines.append("")
        else:
            lines.append(
                "**No edges found.** Agent 2 could not traverse from the root function."
            )
            lines.append(
                "This means the root function name from Neo4j did not match "
                "any indexed function."
            )
            lines.append("")

        if graph.unresolved_callees:
            lines.append(
                f"**Unresolved callees** ({len(graph.unresolved_callees)}):"
            )
            lines.append("")
            for u in graph.unresolved_callees[:30]:
                lines.append(f"- `{u}`")
            lines.append("")

    if not results:
        lines.append("*No flows to display.*")

    return "\n".join(lines)


def _result_to_dict(r: dict) -> dict:
    from trustbot.models.agentic import CallGraphOutput

    res: VerificationResult = r["result"]

    def _edge_list(graph: CallGraphOutput | None) -> list[dict]:
        if not graph:
            return []
        return [
            {
                "caller": e.caller, "callee": e.callee,
                "caller_file": e.caller_file, "callee_file": e.callee_file,
                "caller_class": e.caller_class, "callee_class": e.callee_class,
                "confidence": e.confidence,
                "method": e.extraction_method.value,
            }
            for e in graph.edges
        ]

    neo4j_graph: CallGraphOutput | None = r.get("neo4j_graph")
    index_graph: CallGraphOutput | None = r.get("index_graph")

    return {
        "flow_key": r["flow_key"],
        "flow_name": r["flow_name"],
        "trust_score": res.flow_trust_score,
        "graph_trust_score": res.graph_trust_score,
        "neo4j_edge_count": r["neo4j_edges"],
        "index_edge_count": r["index_edges"],
        "confirmed": len(res.confirmed_edges),
        "phantom": len(res.phantom_edges),
        "missing": len(res.missing_edges),
        "match_tiers": {
            "full_match": res.metadata.get("match_full", 0),
            "name_file_match": res.metadata.get("match_name_file", 0),
            "name_only_match": res.metadata.get("match_name_only", 0),
        },
        "agent1_neo4j": {
            "root_function": neo4j_graph.root_function if neo4j_graph else "",
            "edge_count": len(neo4j_graph.edges) if neo4j_graph else 0,
            "edges": _edge_list(neo4j_graph),
            "metadata": neo4j_graph.metadata if neo4j_graph else {},
        },
        "agent2_index": {
            "root_function": index_graph.root_function if index_graph else "",
            "edge_count": len(index_graph.edges) if index_graph else 0,
            "edges": _edge_list(index_graph),
            "unresolved": index_graph.unresolved_callees if index_graph else [],
            "metadata": index_graph.metadata if index_graph else {},
        },
        "agent3_comparison": {
            "confirmed_edges": [
                {
                    "caller": e.caller, "callee": e.callee,
                    "trust": e.trust_score,
                    "caller_file": e.caller_file, "callee_file": e.callee_file,
                    "match_type": e.details,
                }
                for e in res.confirmed_edges
            ],
            "phantom_edges": [
                {"caller": e.caller, "callee": e.callee, "details": e.details}
                for e in res.phantom_edges
            ],
            "missing_edges": [
                {"caller": e.caller, "callee": e.callee, "details": e.details}
                for e in res.missing_edges
            ],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# NiceGUI Page
# ═══════════════════════════════════════════════════════════════════════════


def create_ui():
    """Build the NiceGUI page. Called once from main.py at import time."""

    @ui.page("/")
    async def index_page():
        global _pipeline, _orchestrator, _git_index

        registry = _get_registry()
        from trustbot.agent.orchestrator import AgentOrchestrator
        from trustbot.agents.pipeline import ValidationPipeline
        from trustbot.index.code_index import CodeIndex

        _orchestrator = AgentOrchestrator(registry)

        git_index_path = settings.codebase_root / ".trustbot_git_index.db"
        if git_index_path.exists() and _git_index is None:
            try:
                _git_index = CodeIndex(db_path=git_index_path)
                logger.info("Auto-loaded existing git index from %s", git_index_path)
            except Exception as e:
                logger.warning("Could not auto-load git index: %s", e)

        try:
            _pipeline = ValidationPipeline(
                neo4j_tool=registry.get("neo4j"),
                code_index=_git_index,
            )
        except KeyError:
            logger.warning("ValidationPipeline not available (missing neo4j tool)")

        # ── Page header ───────────────────────────────────────────────
        ui.markdown("# TrustBot\n*3-Agent call graph validation: Neo4j vs Indexed Codebase*")

        with ui.tabs().classes("w-full") as tabs:
            tab_indexer = ui.tab("1. Code Indexer")
            tab_validate = ui.tab("2. Validate")
            tab_chunks = ui.tab("3. Chunk Visualizer")
            tab_chat = ui.tab("4. Chat")
            tab_mgmt = ui.tab("5. Index Management")

        with ui.tab_panels(tabs, value=tab_indexer).classes("w-full"):

            # ═══════════════════════════════════════════════════════════
            # Tab 1: Code Indexer
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_indexer):
                ui.markdown(
                    "### Step 1: Index Your Codebase\n"
                    "Clone a git repository **or** select a local folder to build a "
                    "code index. This index is used by **Agent 2** during validation "
                    "to independently reconstruct the call graph from source code.\n\n"
                    "After indexing, switch to the **Validate** tab."
                )

                source_radio = ui.radio(
                    ["Git Repository", "Local Folder"],
                    value="Git Repository",
                ).props("inline")

                git_row = ui.row().classes("w-full gap-4")
                with git_row:
                    git_url_input = ui.input(
                        label="Git Repository URL",
                        placeholder="https://github.com/username/repo.git",
                    ).classes("flex-grow-[3]")
                    branch_input = ui.input(
                        label="Branch", placeholder="main", value="main",
                    ).classes("flex-grow")

                local_row = ui.row().classes("w-full gap-4")
                local_row.set_visibility(False)
                with local_row:
                    folder_path_input = ui.input(
                        label="Folder Path",
                        value="/mnt/storage/",
                        placeholder="/mnt/storage/",
                    ).classes("flex-grow")

                def _toggle_source(e):
                    is_local = e.value == "Local Folder"
                    git_row.set_visibility(not is_local)
                    local_row.set_visibility(is_local)

                source_radio.on_value_change(_toggle_source)

                idx_progress = ui.linear_progress(value=0, show_value=False).classes(
                    "w-full"
                )
                idx_progress.set_visibility(False)
                idx_status = ui.markdown("")
                index_btn = ui.button("Index Codebase", color="primary")

                async def _on_index_click():
                    index_btn.disable()
                    idx_progress.set_visibility(True)
                    idx_status.set_content("Indexing...")

                    def _progress(pct, desc):
                        idx_progress.set_value(pct)

                    try:
                        if source_radio.value == "Local Folder":
                            result = await _index_local_folder(
                                folder_path_input.value, _progress,
                            )
                        else:
                            result = await _clone_and_index_repo(
                                git_url_input.value, branch_input.value, _progress,
                            )
                    except Exception as exc:
                        result = f"Error: {exc}"

                    idx_progress.set_value(1.0)
                    idx_status.set_content(result)
                    index_btn.enable()

                index_btn.on_click(_on_index_click)

            # ═══════════════════════════════════════════════════════════
            # Tab 2: Validate
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_validate):
                ui.markdown(
                    "### Step 2: Validate Execution Flows\n"
                    "Enter the Project ID and Run ID from Neo4j. "
                    "The 3-agent pipeline will:\n\n"
                    "1. **Agent 1** -- Fetch call graphs from Neo4j and identify "
                    "ROOT snippets\n"
                    "2. **Agent 2** -- Build call graphs from the indexed codebase "
                    "starting at ROOT\n"
                    "3. **Agent 3** -- Compare both graphs, classify edges, compute "
                    "trust scores"
                )

                with ui.row().classes("w-full gap-4 items-end"):
                    project_id_input = ui.input(
                        label="Project ID", placeholder="e.g. 3151",
                    ).classes("flex-grow")
                    run_id_input = ui.input(
                        label="Run ID", placeholder="e.g. 4912",
                    ).classes("flex-grow")
                    validate_btn = ui.button(
                        "Validate All Flows", color="primary",
                    )

                val_progress = ui.linear_progress(value=0, show_value=False).classes(
                    "w-full"
                )
                val_progress.set_visibility(False)
                val_step_label = ui.label("")

                # Accordions container: hidden until user clicks Validate
                report_section = ui.column().classes("w-full gap-2")
                report_section.set_visibility(False)

                with report_section:
                    summary_md = ui.markdown("")

                    accordion_classes = "w-full rounded-borders bordered q-mb-sm"
                    accordion_header_class = "bg-grey-7 text-white"
                    accordion_body_classes = "bg-white q-pa-md"

                    with ui.expansion("Detailed Report", icon="description").classes(
                        accordion_classes
                    ) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes):
                            report_md = ui.markdown("")

                    with ui.expansion("Call Tree Diagrams -- Mermaid", icon="account_tree").classes(
                        accordion_classes
                    ) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes):
                            mermaid_container = ui.column().classes("w-full gap-4")

                    with ui.expansion(
                        "Call Tree Diagrams -- Interactive DAG", icon="hub"
                    ).classes(accordion_classes) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes):
                            dag_container = ui.column().classes("w-full gap-4")

                    with ui.expansion(
                        "Agent 1 Output (Neo4j Call Graph)", icon="storage"
                    ).classes(accordion_classes) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes):
                            agent1_md = ui.markdown("")

                    with ui.expansion(
                        "Agent 2 Output (Indexed Codebase Call Graph)", icon="code"
                    ).classes(accordion_classes) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes):
                            agent2_md = ui.markdown("")

                    with ui.expansion("Raw JSON", icon="data_object").classes(
                        accordion_classes
                    ) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes):
                            json_editor = ui.code("", language="json").classes("w-full")

                async def _on_validate_click():
                    p_str = (project_id_input.value or "").strip()
                    r_str = (run_id_input.value or "").strip()
                    if not p_str or not r_str:
                        summary_md.set_content("Please enter both Project ID and Run ID.")
                        return

                    try:
                        project_id = int(p_str)
                        run_id = int(r_str)
                    except ValueError:
                        summary_md.set_content("Project ID and Run ID must be integers.")
                        return

                    validate_btn.disable()
                    val_progress.set_visibility(True)
                    val_progress.set_value(0)
                    _set_progress(0.0, "Initializing...", done=False)

                    timer = ui.timer(
                        0.3,
                        lambda: (
                            val_progress.set_value(_get_progress()["pct"]),
                            val_step_label.set_text(_get_progress()["step"]),
                        ),
                    )

                    all_results, error = await _validate_all_flows(project_id, run_id)
                    timer.deactivate()

                    val_progress.set_value(1.0)
                    val_step_label.set_text("Validation complete!")

                    if error:
                        summary_md.set_content(error)
                        validate_btn.enable()
                        return

                    report_section.set_visibility(True)
                    summary_md.set_content(
                        _format_3agent_summary(project_id, run_id, all_results)
                    )
                    report_md.set_content(
                        _format_3agent_report(project_id, run_id, all_results)
                    )
                    agent1_md.set_content(
                        _format_agent_output(
                            "Agent 1 (Neo4j)", all_results, "neo4j_graph"
                        )
                    )
                    agent2_md.set_content(
                        _format_agent_output(
                            "Agent 2 (Index)", all_results, "index_graph"
                        )
                    )
                    raw_json = json.dumps(
                        [_result_to_dict(r) for r in all_results],
                        indent=2, default=str,
                    )
                    json_editor.set_content(raw_json)

                    # Build Mermaid diagrams (native)
                    from trustbot.ui.call_tree import build_echart_dag, build_mermaid

                    mermaid_container.clear()
                    with mermaid_container:
                        for f_idx, r in enumerate(all_results):
                            neo_g = r.get("neo4j_graph")
                            idx_g = r.get("index_graph")
                            trust = r["result"].flow_trust_score
                            neo_mm = build_mermaid(neo_g) if neo_g and neo_g.edges else ""
                            idx_mm = build_mermaid(idx_g) if idx_g and idx_g.edges else ""
                            if not neo_mm and not idx_mm:
                                continue
                            trust_color = (
                                "green" if trust > 0.7
                                else "orange" if trust > 0.3
                                else "red"
                            )
                            ui.label(
                                f"Flow {f_idx+1}: {r['flow_name']}  "
                                f"({trust:.0%} trust)"
                            ).classes(f"text-lg font-bold text-{trust_color}-700")
                            with ui.row().classes("w-full gap-4"):
                                if neo_mm:
                                    with ui.card().classes("flex-1"):
                                        ui.label(
                                            f"Agent 1 -- Neo4j "
                                            f"({len(neo_g.edges)} edges)"
                                        ).classes("text-orange-600 font-bold")
                                        logger.debug(
                                            "Mermaid script (Flow %s, Neo4j):\n%s",
                                            f_idx + 1, neo_mm,
                                        )
                                        ui.mermaid(neo_mm)
                                if idx_mm:
                                    with ui.card().classes("flex-1"):
                                        ui.label(
                                            f"Agent 2 -- Index "
                                            f"({len(idx_g.edges)} edges)"
                                        ).classes("text-purple-600 font-bold")
                                        logger.debug(
                                            "Mermaid script (Flow %s, Index):\n%s",
                                            f_idx + 1, idx_mm,
                                        )
                                        ui.mermaid(idx_mm)
                            ui.separator()

                    # Build ECharts DAG (interactive)
                    dag_container.clear()
                    with dag_container:
                        for f_idx, r in enumerate(all_results):
                            neo_g = r.get("neo4j_graph")
                            idx_g = r.get("index_graph")
                            neo_has = neo_g and neo_g.edges
                            idx_has = idx_g and idx_g.edges
                            if not neo_has and not idx_has:
                                continue
                            ui.label(
                                f"Flow {f_idx+1}: {r['flow_name']}"
                            ).classes("text-lg font-bold")
                            with ui.row().classes("w-full gap-4"):
                                if neo_has:
                                    with ui.card().classes("flex-1"):
                                        ui.label("Agent 1 -- Neo4j").classes(
                                            "text-orange-600 font-bold"
                                        )
                                        ui.echart(
                                            build_echart_dag(neo_g, "Neo4j")
                                        ).classes("w-full h-96")
                                if idx_has:
                                    with ui.card().classes("flex-1"):
                                        ui.label("Agent 2 -- Index").classes(
                                            "text-purple-600 font-bold"
                                        )
                                        ui.echart(
                                            build_echart_dag(idx_g, "Index")
                                        ).classes("w-full h-96")
                            ui.separator()

                    validate_btn.enable()

                validate_btn.on_click(_on_validate_click)

            # ═══════════════════════════════════════════════════════════
            # Tab 3: Chunk Visualizer
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_chunks):
                ui.markdown(
                    "### Code Chunk Graph\n"
                    "Browse all indexed code chunks and their call relationships.\n"
                    "Index a repository first using the **Code Indexer** tab."
                )

                with ui.row().classes("w-full gap-4 items-end"):
                    refresh_btn = ui.button(
                        "Refresh Visualization", color="primary",
                    )
                    page_size_select = ui.select(
                        [25, 50, 100, 200], value=50, label="Rows per page",
                    )

                chunk_stats_md = ui.markdown(
                    "Click **Refresh** after indexing a repository."
                )

                with ui.tabs().classes("w-full") as chunk_tabs:
                    fn_tab = ui.tab("Functions")
                    edge_tab = ui.tab("Call Relationships")

                with ui.tab_panels(chunk_tabs, value=fn_tab).classes("w-full"):
                    with ui.tab_panel(fn_tab):
                        fn_table = ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num", "sortable": True},
                                {"name": "name", "label": "Function", "field": "name", "sortable": True},
                                {"name": "file", "label": "File", "field": "file", "sortable": True},
                                {"name": "lang", "label": "Language", "field": "lang", "sortable": True},
                                {"name": "type", "label": "Type", "field": "type", "sortable": True},
                            ],
                            rows=[],
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")

                    with ui.tab_panel(edge_tab):
                        edge_table = ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num", "sortable": True},
                                {"name": "caller", "label": "Caller", "field": "caller", "sortable": True},
                                {"name": "callee", "label": "Callee", "field": "callee", "sortable": True},
                                {"name": "confidence", "label": "Confidence", "field": "confidence", "sortable": True},
                            ],
                            rows=[],
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")

                async def _on_refresh_viz():
                    refresh_btn.disable()
                    try:
                        from trustbot.indexing.chunk_visualizer import ChunkVisualizer
                        active_index = _git_index
                        viz = ChunkVisualizer(active_index)
                        data = await viz.get_graph_data()
                    except Exception as exc:
                        logger.exception("Chunk visualization failed")
                        chunk_stats_md.set_content(f"Error: {exc}")
                        refresh_btn.enable()
                        return

                    nodes = data.get("nodes", [])
                    edges = data.get("edges", [])

                    ps = page_size_select.value or 50
                    fn_table.update_rows([
                        {
                            "num": str(i),
                            "name": str(n.get("name", "")),
                            "file": str(n.get("file", "")).replace("\\", "/"),
                            "lang": str(n.get("language", "")),
                            "type": str(n.get("type", "function")),
                        }
                        for i, n in enumerate(nodes, 1)
                    ])
                    fn_table.pagination = {"rowsPerPage": ps}

                    edge_table.update_rows([
                        {
                            "num": str(i),
                            "caller": _short_chunk_id(str(e.get("from", ""))),
                            "callee": _short_chunk_id(str(e.get("to", ""))),
                            "confidence": str(e.get("confidence", "")),
                        }
                        for i, e in enumerate(edges, 1)
                    ])
                    edge_table.pagination = {"rowsPerPage": ps}

                    chunk_stats_md.set_content(
                        f"**Total Functions**: {len(nodes)} | "
                        f"**Total Call Relationships**: {len(edges)}"
                    )
                    refresh_btn.enable()

                refresh_btn.on_click(_on_refresh_viz)

            # ═══════════════════════════════════════════════════════════
            # Tab 4: Chat
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_chat):
                ui.markdown(
                    "Ask TrustBot questions about execution flows, code, "
                    "or the knowledge graph."
                )
                chat_input = ui.textarea(
                    label="Your Question", placeholder="Ask TrustBot...",
                ).classes("w-full")
                chat_btn = ui.button("Send", color="primary")
                chat_output = ui.markdown("")

                async def _on_chat_click():
                    msg = (chat_input.value or "").strip()
                    if not msg:
                        chat_output.set_content("Please enter a question.")
                        return
                    chat_btn.disable()
                    chat_output.set_content("*Thinking...*")
                    try:
                        result = await _orchestrator.chat(msg)
                    except Exception as exc:
                        logger.exception("Chat failed")
                        result = f"Error: {exc}"
                    chat_output.set_content(result)
                    chat_btn.enable()

                chat_btn.on_click(_on_chat_click)

            # ═══════════════════════════════════════════════════════════
            # Tab 5: Index Management
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_mgmt):
                ui.markdown("### Codebase Index Management")
                with ui.row().classes("gap-4"):
                    incr_btn = ui.button("Incremental Re-index")
                    full_btn = ui.button("Full Re-index", color="secondary")
                    status_btn = ui.button("Check Status")
                mgmt_output = ui.textarea(
                    label="Result", value="",
                ).classes("w-full").props("readonly outlined")

                async def _on_reindex(force: bool):
                    incr_btn.disable()
                    full_btn.disable()
                    status_btn.disable()
                    try:
                        index_tool = registry.get("index")
                        stats = await index_tool.call("reindex", force=force)
                        mgmt_output.set_value(
                            f"Indexing complete.\n"
                            f"Files processed: {stats['files']}\n"
                            f"Chunks created: {stats['chunks']}\n"
                            f"New chunks: {stats['new']}\n"
                            f"Skipped (unchanged): {stats['skipped']}"
                        )
                    except KeyError:
                        mgmt_output.set_value(
                            "Index tool not available (ChromaDB not loaded)"
                        )
                    except Exception as exc:
                        mgmt_output.set_value(f"Indexing failed: {exc}")
                    incr_btn.enable()
                    full_btn.enable()
                    status_btn.enable()

                async def _on_status():
                    incr_btn.disable()
                    full_btn.disable()
                    status_btn.disable()
                    try:
                        index_tool = registry.get("index")
                        status = await index_tool.call("get_index_status")
                        mgmt_output.set_value(json.dumps(status, indent=2))
                    except KeyError:
                        mgmt_output.set_value(
                            "Index tool not available (ChromaDB not loaded)"
                        )
                    except Exception as exc:
                        mgmt_output.set_value(f"Error: {exc}")
                    incr_btn.enable()
                    full_btn.enable()
                    status_btn.enable()

                incr_btn.on_click(lambda: _on_reindex(False))
                full_btn.on_click(lambda: _on_reindex(True))
                status_btn.on_click(_on_status)
