"""NiceGUI-based web UI for TrustBot with 3-agent validation pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

from nicegui import app, ui

from trustbot.config import settings
from trustbot.models.agentic import VerificationResult

logger = logging.getLogger("trustbot.ui")

# ---------------------------------------------------------------------------
# Lazy access to the registry (initialised at startup in main.py)
# ---------------------------------------------------------------------------


def _get_registry():
    from trustbot.main import get_registry
    return get_registry()


async def _get_registry_async():
    from trustbot.main import wait_for_registry
    return await wait_for_registry()


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
        # --- Agent 0: Language Intelligence (auto-detect & generate profiles) ---
        if progress_cb:
            progress_cb(0.02, "Agent 0: Detecting languages...")

        from trustbot.agents.agent0_language import Agent0LanguageProfiler
        from trustbot.indexing.chunker import set_language_profiles

        agent0 = Agent0LanguageProfiler(folder)
        profiles = await agent0.run(
            progress_callback=lambda _, msg: progress_cb(0.05, f"Agent 0: {msg}") if progress_cb else None,
        )

        lang_summary = ", ".join(f"{k} ({p.source_file_count} files)" for k, p in profiles.items())
        logger.info("Agent 0 complete: %s", lang_summary)

        if profiles:
            set_language_profiles(profiles)

        if progress_cb:
            progress_cb(0.15, f"Detected {len(profiles)} language(s), chunking...")

        # --- Chunking & call graph building ---
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
            progress_cb(0.55, f"Building call graph from {function_count} functions (LLM extraction)...")

        cache_conn = code_idx.get_cache_conn()
        edges = await build_call_graph_from_chunks(chunks, cache_conn=cache_conn)

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

        profile_lines = "\n".join(
            f"  - **{lang}**: {p.source_file_count} files, "
            f"{len(p.function_def_patterns)} func patterns, "
            f"{'%.0f' % (p.validation_coverage * 100)}% coverage"
            for lang, p in profiles.items()
        )

        return (
            f"## Indexing Complete!\n\n"
            f"**Source**: Local Folder\n"
            f"**Path**: `{folder}`\n\n"
            f"### Agent 0 — Language Profiles\n"
            f"{profile_lines}\n\n"
            f"### Indexing Results\n"
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

        flow_name_map = {f.key: (f.name or f.key) for f in flows}
        flow_keys = [f.key for f in flows]
        _completed = {"count": 0}
        _active_flows: dict[int, str] = {}

        def _parallel_progress(idx, total, agent, msg):
            labels = {"agent1": "Agent 1", "agent2": "Agent 2", "agent3": "Agent 3"}
            label = labels.get(agent, agent)
            flow_name = flow_name_map.get(
                flow_keys[idx] if idx < len(flow_keys) else "", f"Flow {idx+1}"
            )

            if agent == "done":
                _completed["count"] += 1
                _active_flows.pop(idx, None)
                done = _completed["count"]
                pct = 0.10 + 0.80 * (done / total)
                _set_progress(pct, f"Completed {done}/{total} flows")
                return

            _active_flows[idx] = f"{flow_name}: {label}"
            done = _completed["count"]
            pct = 0.10 + 0.80 * (done / total)
            active_summary = ", ".join(
                sorted(_active_flows.values())[:3]
            )
            if len(_active_flows) > 3:
                active_summary += f" (+{len(_active_flows) - 3} more)"
            _set_progress(
                pct,
                f"Done {done}/{total} | Active: {active_summary}"
            )

        if hasattr(_pipeline, "validate_flows"):
            raw_results = await _pipeline.validate_flows(
                flow_keys,
                progress_callback=_parallel_progress,
            )
        else:
            raw_results = []
            for idx, key in enumerate(flow_keys):
                def _seq_progress(agent, msg, _idx=idx):
                    _parallel_progress(_idx, total_flows, agent, msg)
                r = await _pipeline.validate_flow(key, progress_callback=_seq_progress)
                raw_results.append(r)
                _parallel_progress(idx, total_flows, "done", "")

        all_results: list[dict] = []
        for key, (result, report_md, neo4j_g, index_g) in zip(flow_keys, raw_results):
            all_results.append({
                "flow_key": key,
                "flow_name": flow_name_map.get(key, key),
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
    total_coverage_gaps = sum(len(r["result"].codebase_extra_edges) for r in results)
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
        f"  - Codebase coverage gaps: {total_coverage_gaps}",
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
        "# Agent Validation Report",
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
        lines.append(f"| Codebase coverage gaps | {len(res.codebase_extra_edges)} |")
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

        if res.codebase_extra_edges:
            lines += [
                "### Codebase Coverage Gaps",
                "",
                "Edges found in the indexed codebase for functions in the Neo4j "
                "call tree, but **not present in Neo4j**. These are real calls "
                "in the code that Neo4j may be missing.",
                "",
                "| # | Caller | Callee | Caller File | Callee File | Confidence |",
                "|---|--------|--------|-------------|-------------|------------|",
            ]
            for i, e in enumerate(res.codebase_extra_edges[:30], 1):
                cr_file = (e.caller_file or "").replace("\\", "/").rsplit("/", 1)[-1] if e.caller_file else "-"
                ce_file = (e.callee_file or "").replace("\\", "/").rsplit("/", 1)[-1] if e.callee_file else "-"
                lines.append(
                    f"| {i} | `{e.caller}` | `{e.callee}` "
                    f"| {cr_file} | {ce_file} | {e.trust_score:.2f} |"
                )
            if len(res.codebase_extra_edges) > 30:
                lines.append(
                    f"| ... | +{len(res.codebase_extra_edges) - 30} more | | | | |"
                )
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
# DB Entity Checker — render helpers
# ═══════════════════════════════════════════════════════════════════════════

_STATUS_COLORS = {
    "MATCHED": "green",
    "ONLY_IN_DB": "orange",
    "ONLY_IN_NEO4J": "red",
    "TYPE_MISMATCH": "yellow-9",
}

_STATUS_BG = {
    "MATCHED": "bg-green-1",
    "ONLY_IN_DB": "bg-orange-1",
    "ONLY_IN_NEO4J": "bg-red-1",
    "TYPE_MISMATCH": "bg-yellow-1",
}


def _render_db_entity_results(
    summary,
    db_tables,
    neo4j_entities,
    neo4j_warning,
    summary_cards_row,
    summary_table_container,
    db_tables_container,
    neo4j_entities_container,
    discrepancies_container,
):
    """Populate all four result sub-tabs for the DB Entity Checker."""
    from trustbot.models.db_entity import SchemaComparisonSummary

    # ---- Summary sub-tab ----
    summary_cards_row.clear()
    with summary_cards_row:
        for label, value, color in [
            ("Total Tables", summary.total_tables, "blue"),
            ("Matched", summary.matched_tables, "green"),
            ("Only in DB", summary.only_in_db, "orange"),
            ("Only in Neo4j", summary.only_in_neo4j, "red"),
        ]:
            with ui.card().classes(f"q-pa-sm items-center").style(
                "min-width: 120px;"
            ):
                ui.label(str(value)).classes(
                    f"text-h4 text-weight-bold text-{color}"
                )
                ui.label(label).classes("text-caption text-grey-8")

    summary_table_container.clear()
    with summary_table_container:
        if neo4j_warning:
            ui.label(neo4j_warning).classes("text-orange-8 q-mb-sm")

        if summary.matched_tables > 0:
            col_summary_text = (
                f"**Column-level** (across {summary.matched_tables} matched tables): "
                f"{summary.matched_columns} matched, "
                f"{summary.columns_only_in_db} DB-only, "
                f"{summary.columns_only_in_neo4j} Neo4j-only, "
                f"{summary.type_mismatches} type mismatches"
            )
            ui.markdown(col_summary_text)

        rows = []
        for idx, r in enumerate(summary.results, 1):
            col_info = ""
            if r.status == "MATCHED" and r.column_discrepancies:
                matched_c = sum(
                    1 for d in r.column_discrepancies if d.status == "MATCHED"
                )
                total_c = len(r.column_discrepancies)
                issues = total_c - matched_c
                col_info = (
                    f"{matched_c}/{total_c} columns matched"
                    + (f", {issues} issue(s)" if issues else "")
                )
            rows.append({
                "num": idx,
                "table": r.table_name,
                "status": r.status,
                "db_cols": len(r.db_columns),
                "neo4j_fields": len(r.neo4j_fields),
                "col_info": col_info,
            })

        ui.table(
            columns=[
                {"name": "num", "label": "#", "field": "num", "sortable": True},
                {"name": "table", "label": "Table Name", "field": "table", "sortable": True},
                {"name": "status", "label": "Status", "field": "status", "sortable": True},
                {"name": "db_cols", "label": "DB Columns", "field": "db_cols", "sortable": True},
                {"name": "neo4j_fields", "label": "Neo4j Fields", "field": "neo4j_fields", "sortable": True},
                {"name": "col_info", "label": "Column Details", "field": "col_info"},
            ],
            rows=rows,
            pagination={"rowsPerPage": 25},
        ).classes("w-full")

    # ---- Database Tables sub-tab ----
    db_tables_container.clear()
    with db_tables_container:
        if not db_tables:
            ui.label("No tables found in the database schema.").classes(
                "text-grey-7"
            )
        else:
            ui.label(
                f"{len(db_tables)} table(s) from database"
            ).classes("text-lg text-weight-bold q-mb-sm")
            for tbl in db_tables:
                with ui.expansion(
                    f"{tbl.name} ({len(tbl.columns)} columns)"
                ).classes("w-full bordered rounded-borders q-mb-xs"):
                    if tbl.columns:
                        col_rows = []
                        for i, c in enumerate(tbl.columns, 1):
                            pk_str = "PK" if c.is_primary_key else ""
                            null_str = "NULL" if c.is_nullable else "NOT NULL"
                            col_rows.append({
                                "num": i,
                                "name": c.name,
                                "type": c.data_type,
                                "nullable": null_str,
                                "pk": pk_str,
                            })
                        ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num"},
                                {"name": "name", "label": "Column", "field": "name", "sortable": True},
                                {"name": "type", "label": "Type", "field": "type"},
                                {"name": "nullable", "label": "Nullable", "field": "nullable"},
                                {"name": "pk", "label": "PK", "field": "pk"},
                            ],
                            rows=col_rows,
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")
                    else:
                        ui.label("No columns.").classes("text-grey-6")

    # ---- Neo4j Entities sub-tab ----
    neo4j_entities_container.clear()
    with neo4j_entities_container:
        if neo4j_warning:
            ui.label(neo4j_warning).classes("text-orange-8 q-mb-sm")
        if not neo4j_entities:
            ui.label("No DatabaseEntity nodes found.").classes("text-grey-7")
        else:
            ui.label(
                f"{len(neo4j_entities)} DatabaseEntity node(s) from Neo4j"
            ).classes("text-lg text-weight-bold q-mb-sm")
            for ent in neo4j_entities:
                with ui.expansion(
                    f"{ent.name} ({len(ent.fields)} fields)"
                ).classes("w-full bordered rounded-borders q-mb-xs"):
                    if ent.fields:
                        field_rows = []
                        for i, f in enumerate(ent.fields, 1):
                            pk_str = "PK" if f.is_primary_key else ""
                            null_str = "NULL" if f.is_nullable else "NOT NULL"
                            field_rows.append({
                                "num": i,
                                "name": f.name,
                                "type": f.data_type,
                                "nullable": null_str,
                                "pk": pk_str,
                            })
                        ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num"},
                                {"name": "name", "label": "Field", "field": "name", "sortable": True},
                                {"name": "type", "label": "Type", "field": "type"},
                                {"name": "nullable", "label": "Nullable", "field": "nullable"},
                                {"name": "pk", "label": "PK", "field": "pk"},
                            ],
                            rows=field_rows,
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")
                    else:
                        ui.label("No fields.").classes("text-grey-6")

    # ---- Discrepancies sub-tab ----
    discrepancies_container.clear()
    with discrepancies_container:
        issues = [
            r for r in summary.results
            if r.status != "MATCHED"
            or any(d.status != "MATCHED" for d in r.column_discrepancies)
        ]
        if not issues:
            ui.label(
                "No discrepancies found. All tables and columns match."
            ).classes("text-green-8 text-lg text-weight-bold")
        else:
            ui.label(
                f"{len(issues)} table(s) with discrepancies"
            ).classes("text-lg text-weight-bold text-red-8 q-mb-sm")

            for r in issues:
                badge_color = _STATUS_COLORS.get(r.status, "grey")
                with ui.expansion(
                    f"{r.table_name}"
                ).classes(
                    f"w-full bordered rounded-borders q-mb-xs {_STATUS_BG.get(r.status, '')}"
                ):
                    with ui.row().classes("items-center gap-2 q-mb-sm"):
                        ui.badge(r.status, color=badge_color)
                        if r.status == "ONLY_IN_DB":
                            ui.label(
                                f"Table exists in database ({len(r.db_columns)} columns) "
                                f"but NOT in Neo4j."
                            )
                        elif r.status == "ONLY_IN_NEO4J":
                            ui.label(
                                f"Table exists in Neo4j ({len(r.neo4j_fields)} fields) "
                                f"but NOT in database."
                            )
                        else:
                            col_issues = [
                                d for d in r.column_discrepancies
                                if d.status != "MATCHED"
                            ]
                            ui.label(
                                f"Table matched but {len(col_issues)} column-level "
                                f"discrepanc{'y' if len(col_issues) == 1 else 'ies'} found."
                            )

                    if r.status == "MATCHED" and r.column_discrepancies:
                        disc_rows = []
                        for i, d in enumerate(r.column_discrepancies, 1):
                            disc_rows.append({
                                "num": i,
                                "column": d.column_name,
                                "status": d.status,
                                "db_type": d.db_type or "-",
                                "neo4j_type": d.neo4j_type or "-",
                            })
                        ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num"},
                                {"name": "column", "label": "Column", "field": "column", "sortable": True},
                                {"name": "status", "label": "Status", "field": "status", "sortable": True},
                                {"name": "db_type", "label": "DB Type", "field": "db_type"},
                                {"name": "neo4j_type", "label": "Neo4j Type", "field": "neo4j_type"},
                            ],
                            rows=disc_rows,
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")

                    elif r.status == "ONLY_IN_DB" and r.db_columns:
                        ui.label("Columns in database:").classes("text-weight-bold")
                        col_rows = []
                        for i, c in enumerate(r.db_columns, 1):
                            col_rows.append({
                                "num": i,
                                "name": c.name,
                                "type": c.data_type,
                            })
                        ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num"},
                                {"name": "name", "label": "Column", "field": "name"},
                                {"name": "type", "label": "Type", "field": "type"},
                            ],
                            rows=col_rows,
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")

                    elif r.status == "ONLY_IN_NEO4J" and r.neo4j_fields:
                        ui.label("Fields in Neo4j:").classes("text-weight-bold")
                        field_rows = []
                        for i, f in enumerate(r.neo4j_fields, 1):
                            field_rows.append({
                                "num": i,
                                "name": f.name,
                                "type": f.data_type,
                            })
                        ui.table(
                            columns=[
                                {"name": "num", "label": "#", "field": "num"},
                                {"name": "name", "label": "Field", "field": "name"},
                                {"name": "type", "label": "Type", "field": "type"},
                            ],
                            rows=field_rows,
                            pagination={"rowsPerPage": 50},
                        ).classes("w-full")


# ═══════════════════════════════════════════════════════════════════════════
# NiceGUI Page
# ═══════════════════════════════════════════════════════════════════════════


def create_ui():
    """Build the NiceGUI page. Called once from main.py at import time."""

    @ui.page("/")
    async def index_page():
        global _pipeline, _orchestrator, _git_index

        registry = await _get_registry_async()
        from trustbot.agent.orchestrator import AgentOrchestrator
        from trustbot.agents.pipeline import create_pipeline
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
            fs_tool = registry.get("filesystem") if registry else None
        except (KeyError, Exception):
            fs_tool = None

        try:
            _pipeline = create_pipeline(
                neo4j_tool=registry.get("neo4j"),
                code_index=_git_index,
                filesystem_tool=fs_tool,
            )
        except KeyError:
            logger.warning("ValidationPipeline not available (missing neo4j tool)")

        # ── Page header ───────────────────────────────────────────────
        _mode_label = "LLM Agentic" if settings.agentic_mode == "llm" else "Rule-Based"
        ui.markdown(
            f"# TrustBot\n"
            f"*3-Agent call graph validation: Neo4j vs Indexed Codebase*\n\n"
            f"**Mode:** {_mode_label} (`TRUSTBOT_AGENTIC_MODE={settings.agentic_mode}`)"
        )

        with ui.tabs().classes("w-full") as tabs:
            tab_indexer = ui.tab("1. Code Indexer")
            tab_validate = ui.tab("2. Validate")
            tab_chunks = ui.tab("3. Chunk Visualizer")
            tab_chat = ui.tab("4. Chat")
            tab_mgmt = ui.tab("5. Index Management")
            tab_db_entity = ui.tab("6. DB Entity Checker")
            tab_topic_conv = ui.tab("7. Topic Convergence")
            tab_chonkie = ui.tab("8. Chonkie Chunk POC")

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
                        try:
                            idx_progress.set_value(pct)
                        except RuntimeError:
                            pass

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

                    try:
                        idx_progress.set_value(1.0)
                        idx_status.set_content(result)
                        index_btn.enable()
                    except RuntimeError:
                        pass

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
                        with ui.element("div").classes(accordion_body_classes).style(
                            "width: 100%; box-sizing: border-box;"
                        ):
                            mermaid_container = ui.column().style(
                                "width: 100%; gap: 1rem;"
                            )

                    with ui.expansion(
                        "Call Tree Diagrams -- Interactive DAG", icon="hub"
                    ).classes(accordion_classes) as exp:
                        exp.props["header-class"] = accordion_header_class
                        with ui.element("div").classes(accordion_body_classes).style(
                            "width: 100%; box-sizing: border-box;"
                        ):
                            dag_container = ui.column().style(
                                "width: 100%; gap: 1rem;"
                            )

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

                # Restore persisted state after reconnect/lock (Option A + B)
                if settings.storage_secret:
                    try:
                        user = app.storage.user
                        pid = user.get("last_project_id")
                        rid = user.get("last_run_id")
                        if pid is not None and rid is not None:
                            project_id_input.value = str(pid)
                            run_id_input.value = str(rid)
                        summary = user.get("last_summary")
                        if summary:
                            summary_md.set_content(summary)
                            report_section.set_visibility(True)
                        report = user.get("last_report")
                        if report:
                            report_md.set_content(report)
                        agent1 = user.get("last_agent1_output")
                        if agent1:
                            agent1_md.set_content(agent1)
                        agent2 = user.get("last_agent2_output")
                        if agent2:
                            agent2_md.set_content(agent2)
                        raw_json_str = user.get("last_raw_json")
                        if raw_json_str:
                            json_editor.set_content(raw_json_str)
                    except RuntimeError as e:
                        logger.debug("Could not restore validation state: %s", e)

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
                            with ui.element("div").style(
                                "display: grid; grid-template-columns: 1fr 1fr; "
                                "gap: 1rem; width: 100%; overflow: hidden;"
                            ).classes("w-full"):
                                if neo_mm:
                                    with ui.card().classes("w-full").style(
                                        "min-width: 0; overflow: hidden;"
                                    ):
                                        ui.label(
                                            f"Agent 1 -- Neo4j "
                                            f"({len(neo_g.edges)} edges)"
                                        ).classes("text-orange-600 font-bold")
                                        logger.debug(
                                            "Mermaid script (Flow %s, Neo4j):\n%s",
                                            f_idx + 1, neo_mm,
                                        )
                                        ui.mermaid(neo_mm).classes("w-full").style(
                                            "overflow: auto;"
                                        )
                                if idx_mm:
                                    with ui.card().classes("w-full").style(
                                        "min-width: 0; overflow: hidden;"
                                    ):
                                        ui.label(
                                            f"Agent 2 -- Index "
                                            f"({len(idx_g.edges)} edges)"
                                        ).classes("text-purple-600 font-bold")
                                        logger.debug(
                                            "Mermaid script (Flow %s, Index):\n%s",
                                            f_idx + 1, idx_mm,
                                        )
                                        ui.mermaid(idx_mm).classes("w-full").style(
                                            "overflow: auto;"
                                        )
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
                            with ui.element("div").style(
                                "display: grid; grid-template-columns: 1fr 1fr; "
                                "gap: 1rem; width: 100%; overflow: hidden;"
                            ).classes("w-full"):
                                if neo_has:
                                    with ui.card().classes("w-full").style(
                                        "min-width: 0; overflow: hidden;"
                                    ):
                                        ui.label("Agent 1 -- Neo4j").classes(
                                            "text-orange-600 font-bold"
                                        )
                                        ui.echart(
                                            build_echart_dag(neo_g, "Neo4j")
                                        ).classes("w-full").style("height: 500px;")
                                if idx_has:
                                    with ui.card().classes("w-full").style(
                                        "min-width: 0; overflow: hidden;"
                                    ):
                                        ui.label("Agent 2 -- Index").classes(
                                            "text-purple-600 font-bold"
                                        )
                                        ui.echart(
                                            build_echart_dag(idx_g, "Index")
                                        ).classes("w-full").style("height: 500px;")
                            ui.separator()

                    # Persist state for restore after reconnect/lock (Option A + B)
                    if settings.storage_secret:
                        try:
                            user = app.storage.user
                            user["last_project_id"] = project_id
                            user["last_run_id"] = run_id
                            user["last_summary"] = _format_3agent_summary(
                                project_id, run_id, all_results
                            )
                            user["last_report"] = _format_3agent_report(
                                project_id, run_id, all_results
                            )
                            user["last_agent1_output"] = _format_agent_output(
                                "Agent 1 (Neo4j)", all_results, "neo4j_graph"
                            )
                            user["last_agent2_output"] = _format_agent_output(
                                "Agent 2 (Index)", all_results, "index_graph"
                            )
                            user["last_raw_json"] = raw_json
                        except RuntimeError as e:
                            logger.debug("Could not persist validation state: %s", e)

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

            # ═══════════════════════════════════════════════════════════
            # Tab 6: DB Entity Checker
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_db_entity):
                ui.markdown(
                    "### Database Entity Verification\n"
                    "Verify that database entities in Neo4j match the actual "
                    "database schema. Connect to PostgreSQL or upload a flat "
                    "file containing schema information, then compare against "
                    "`DatabaseEntity` / `DatabaseField` nodes in Neo4j."
                )

                with ui.row().classes("w-full gap-4 items-end"):
                    db_project_id_input = ui.input(
                        label="Project ID", placeholder="e.g. 3151",
                    ).classes("flex-grow")
                    db_run_id_input = ui.input(
                        label="Run ID", placeholder="e.g. 4912",
                    ).classes("flex-grow")

                db_source_radio = ui.radio(
                    ["Flat File Upload" , "PostgreSQL Connection" ],
                    value="Flat File Upload",
                ).props("inline")

                # -- PostgreSQL credential card --
                pg_card = ui.card().classes("w-full q-pa-md")
                pg_card.set_visibility(False)
                with pg_card:
                    with ui.row().classes("w-full gap-4"):
                        pg_host_input = ui.input(
                            label="Host", placeholder="db-server.example.com",
                        ).classes("flex-grow-[3]")
                        pg_port_input = ui.input(
                            label="Port", value="5432", placeholder="5432",
                        ).classes("flex-grow")
                    with ui.row().classes("w-full gap-4"):
                        pg_db_input = ui.input(
                            label="Database", placeholder="mydb",
                        ).classes("flex-grow")
                        pg_schema_input = ui.input(
                            label="Schema", value="public", placeholder="public",
                        ).classes("flex-grow")
                    with ui.row().classes("w-full gap-4"):
                        pg_user_input = ui.input(
                            label="Username", placeholder="admin",
                        ).classes("flex-grow")
                        pg_pass_input = ui.input(
                            label="Password", placeholder="password",
                            password=True, password_toggle_button=True,
                        ).classes("flex-grow")

                # -- Flat file upload card --
                ff_card = ui.card().classes("w-full q-pa-md")
                _uploaded_file: dict = {"name": "", "file": None}

                with ff_card:
                    def _handle_upload(e):
                        _uploaded_file["name"] = e.file.name
                        _uploaded_file["file"] = e.file
                        ff_upload_status.set_text(
                            f"Uploaded: {e.file.name} "
                            f"({e.file.size():,} bytes)"
                        )

                    ui.upload(
                        label="Upload schema file (.csv, .json, .xlsx)",
                        auto_upload=True,
                        on_upload=_handle_upload,
                    ).props('accept=".csv,.json,.xlsx"').classes("w-full")
                    ff_upload_status = ui.label("")
                    ui.markdown(
                        "**Supported formats:** CSV, JSON, Excel (.xlsx)"
                    )
                    with ui.expansion("Sample CSV format").classes("w-full"):
                        ui.code(
                            "table_name,column_name,data_type,is_nullable,is_primary_key\n"
                            "users,id,integer,false,true\n"
                            "users,name,varchar,false,false\n"
                            "users,email,varchar,true,false\n"
                            "orders,id,integer,false,true\n"
                            "orders,user_id,integer,false,false\n"
                            "orders,total,numeric,false,false",
                            language="csv",
                        )
                    with ui.expansion("Sample JSON format").classes("w-full"):
                        ui.code(
                            '{\n'
                            '  "tables": [\n'
                            '    {\n'
                            '      "name": "users",\n'
                            '      "columns": [\n'
                            '        {"name": "id", "data_type": "integer", '
                            '"is_nullable": false, "is_primary_key": true},\n'
                            '        {"name": "name", "data_type": "varchar", '
                            '"is_nullable": false, "is_primary_key": false}\n'
                            '      ]\n'
                            '    }\n'
                            '  ]\n'
                            '}',
                            language="json",
                        )

                def _toggle_db_source(e):
                    is_pg = e.value == "PostgreSQL Connection"
                    pg_card.set_visibility(is_pg)
                    ff_card.set_visibility(not is_pg)

                db_source_radio.on_value_change(_toggle_db_source)

                db_compare_btn = ui.button("Compare Entities", color="primary")
                db_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
                db_progress.set_visibility(False)
                db_status_label = ui.label("")

                # -- Results section (hidden until comparison runs) --
                db_results_section = ui.column().classes("w-full gap-2")
                db_results_section.set_visibility(False)

                with db_results_section:
                    with ui.tabs().classes("w-full") as db_result_tabs:
                        db_tab_summary = ui.tab("Summary")
                        db_tab_db_tables = ui.tab("Database Tables")
                        db_tab_neo4j = ui.tab("Neo4j Entities")
                        db_tab_discrepancies = ui.tab("Discrepancies")

                    with ui.tab_panels(db_result_tabs, value=db_tab_summary).classes("w-full"):

                        # -- Summary sub-tab --
                        with ui.tab_panel(db_tab_summary):
                            db_summary_cards = ui.row().classes("w-full gap-4 q-mb-md")
                            db_summary_table_container = ui.column().classes("w-full")

                        # -- Database Tables sub-tab --
                        with ui.tab_panel(db_tab_db_tables):
                            db_tables_container = ui.column().classes("w-full")

                        # -- Neo4j Entities sub-tab --
                        with ui.tab_panel(db_tab_neo4j):
                            neo4j_entities_container = ui.column().classes("w-full")

                        # -- Discrepancies sub-tab --
                        with ui.tab_panel(db_tab_discrepancies):
                            discrepancies_container = ui.column().classes("w-full")

                async def _on_compare_entities():
                    from trustbot.models.db_entity import (
                        SchemaComparisonSummary,
                    )

                    p_str = (db_project_id_input.value or "").strip()
                    r_str = (db_run_id_input.value or "").strip()
                    if not p_str or not r_str:
                        db_status_label.set_text(
                            "Please enter both Project ID and Run ID."
                        )
                        return
                    try:
                        project_id = int(p_str)
                        run_id = int(r_str)
                    except ValueError:
                        db_status_label.set_text(
                            "Project ID and Run ID must be integers."
                        )
                        return

                    db_compare_btn.disable()
                    db_progress.set_visibility(True)
                    db_progress.set_value(0.0)
                    db_status_label.set_text("Starting comparison...")

                    # --- Step 1: Fetch DB schema ---
                    db_tables = []
                    try:
                        if db_source_radio.value == "PostgreSQL Connection":
                            host = (pg_host_input.value or "").strip()
                            port_str = (pg_port_input.value or "5432").strip()
                            database = (pg_db_input.value or "").strip()
                            schema = (pg_schema_input.value or "public").strip()
                            username = (pg_user_input.value or "").strip()
                            password = pg_pass_input.value or ""

                            if not host:
                                db_status_label.set_text("Please enter the database host.")
                                db_compare_btn.enable()
                                db_progress.set_visibility(False)
                                return
                            if not database:
                                db_status_label.set_text("Please enter the database name.")
                                db_compare_btn.enable()
                                db_progress.set_visibility(False)
                                return
                            if not username:
                                db_status_label.set_text("Please enter the database username.")
                                db_compare_btn.enable()
                                db_progress.set_visibility(False)
                                return

                            db_status_label.set_text(
                                f"Connecting to PostgreSQL at {host}:{port_str}/{database}..."
                            )
                            db_progress.set_value(0.1)

                            from trustbot.tools.db_schema_tool import fetch_pg_schema
                            db_tables = await fetch_pg_schema(
                                host=host,
                                port=int(port_str),
                                database=database,
                                schema=schema,
                                username=username,
                                password=password,
                            )

                        else:
                            if not _uploaded_file["file"]:
                                db_status_label.set_text("Please upload a schema file.")
                                db_compare_btn.enable()
                                db_progress.set_visibility(False)
                                return

                            db_status_label.set_text(
                                f"Parsing {_uploaded_file['name']}..."
                            )
                            db_progress.set_value(0.1)

                            file_content = await _uploaded_file["file"].read()
                            from trustbot.tools.db_schema_tool import parse_schema_file
                            db_tables = parse_schema_file(
                                _uploaded_file["name"],
                                file_content,
                            )

                    except Exception as exc:
                        logger.exception("Failed to fetch DB schema")
                        db_status_label.set_text(f"Error fetching DB schema: {exc}")
                        db_compare_btn.enable()
                        db_progress.set_visibility(False)
                        return

                    db_progress.set_value(0.4)
                    db_status_label.set_text(
                        f"Fetched {len(db_tables)} tables from DB. "
                        f"Querying Neo4j for DatabaseEntity nodes..."
                    )

                    # --- Step 2: Fetch Neo4j entities ---
                    neo4j_entities = []
                    neo4j_warning = ""
                    try:
                        neo4j_tool = registry.get("neo4j")
                        from trustbot.tools.neo4j_entity_tool import (
                            fetch_database_entities,
                        )
                        neo4j_entities = await fetch_database_entities(
                            driver=neo4j_tool.driver,
                            project_id=project_id,
                            run_id=run_id,
                        )
                        if not neo4j_entities:
                            neo4j_warning = (
                                f"No DatabaseEntity nodes found for "
                                f"project_id={project_id}, run_id={run_id}."
                            )
                    except KeyError:
                        neo4j_warning = (
                            "Neo4j tool not available. Only DB schema will be displayed."
                        )
                    except Exception as exc:
                        logger.exception("Failed to query Neo4j entities")
                        neo4j_warning = f"Neo4j query failed: {exc}"

                    db_progress.set_value(0.7)
                    db_status_label.set_text("Comparing schemas...")

                    # --- Step 3: Compare ---
                    from trustbot.services.schema_comparator import compare_schemas
                    summary = compare_schemas(db_tables, neo4j_entities)

                    db_progress.set_value(0.9)
                    db_status_label.set_text("Rendering results...")

                    # --- Step 4: Render results ---
                    _render_db_entity_results(
                        summary,
                        db_tables,
                        neo4j_entities,
                        neo4j_warning,
                        db_summary_cards,
                        db_summary_table_container,
                        db_tables_container,
                        neo4j_entities_container,
                        discrepancies_container,
                    )

                    db_results_section.set_visibility(True)
                    db_progress.set_value(1.0)
                    status_msg = (
                        f"Comparison complete: {summary.total_tables} tables "
                        f"({summary.matched_tables} matched, "
                        f"{summary.only_in_db} DB-only, "
                        f"{summary.only_in_neo4j} Neo4j-only)"
                    )
                    if neo4j_warning:
                        status_msg += f"  |  {neo4j_warning}"
                    db_status_label.set_text(status_msg)
                    db_compare_btn.enable()

                db_compare_btn.on_click(_on_compare_entities)

            # ═══════════════════════════════════════════════════════════
            # Tab 7: Topic Convergence
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_topic_conv):
                ui.markdown(
                    "### Topic Convergence Analysis\n"
                    "Analyze `topic` fields on **all** Neo4j node types "
                    "(Snippet, DBCall, Calculation, ServiceCall, Variable, "
                    "Job, Step, DatabaseEntity, etc.) for a given project.\n\n"
                    "**Detects:** duplicate/similar topics, verb-noun violations, "
                    "topic↔business_summary misalignment, journey chain breaks, "
                    "and missing topics.\n"
                    "**Remedials:** LLM-generated suggestions with one-click "
                    "write-back to Neo4j and full audit log."
                )

                with ui.row().classes("w-full gap-4 items-end"):
                    tc_project_input = ui.input(
                        label="Project ID", placeholder="e.g. 976",
                    ).classes("flex-grow")
                    tc_run_input = ui.input(
                        label="Run ID", placeholder="e.g. 2416",
                    ).classes("flex-grow")

                tc_analyze_btn = ui.button(
                    "Analyze Topics", color="primary",
                ).classes("q-mt-sm")
                tc_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
                tc_progress.set_visibility(False)
                tc_status_label = ui.label("")

                tc_results_section = ui.column().classes("w-full gap-2")
                tc_results_section.set_visibility(False)

                with tc_results_section:
                    with ui.tabs().classes("w-full") as tc_tabs:
                        tc_tab_summary = ui.tab("Summary")
                        tc_tab_all = ui.tab("All Nodes")
                        tc_tab_dups = ui.tab("Duplicate Groups")
                        tc_tab_journey = ui.tab("Journey Chains")
                        tc_tab_audit = ui.tab("Audit Log")

                    with ui.tab_panels(tc_tabs, value=tc_tab_summary).classes("w-full"):

                        # ── Summary sub-tab ──
                        tc_summary_container = ui.column().classes("w-full")
                        with ui.tab_panel(tc_tab_summary):
                            tc_summary_inner = ui.column().classes("w-full gap-2")

                        # ── All Nodes sub-tab ──
                        with ui.tab_panel(tc_tab_all):
                            tc_all_filter_row = ui.row().classes("w-full gap-2 items-center")
                            with tc_all_filter_row:
                                tc_filter_type = ui.select(
                                    label="Node Type",
                                    options=["All"],
                                    value="All",
                                ).classes("w-40")
                                tc_filter_issue = ui.select(
                                    label="Issue",
                                    options=["All", "duplicate", "similar", "verb_noun",
                                             "misaligned", "journey_break", "technical_glue",
                                             "topic_missing"],
                                    value="All",
                                ).classes("w-40")
                                tc_apply_selected_btn = ui.button(
                                    "Apply All Selected", color="positive",
                                ).props("outline")
                            tc_all_table_container = ui.column().classes("w-full")

                        # ── Duplicate Groups sub-tab ──
                        with ui.tab_panel(tc_tab_dups):
                            tc_dups_container = ui.column().classes("w-full gap-2")

                        # ── Journey Chains sub-tab ──
                        with ui.tab_panel(tc_tab_journey):
                            tc_journey_container = ui.column().classes("w-full gap-2")

                        # ── Audit Log sub-tab ──
                        with ui.tab_panel(tc_tab_audit):
                            tc_audit_btn_row = ui.row().classes("gap-2")
                            with tc_audit_btn_row:
                                tc_export_json_btn = ui.button(
                                    "Export JSON", color="secondary",
                                ).props("outline size=sm")
                                tc_export_csv_btn = ui.button(
                                    "Export CSV", color="secondary",
                                ).props("outline size=sm")
                                tc_undo_all_btn = ui.button(
                                    "Undo All", color="negative",
                                ).props("outline size=sm")
                            tc_audit_container = ui.column().classes("w-full gap-2")

                # ── State holders ──
                _tc_report = {"data": None}
                _tc_selected_keys: set = set()

                # ── Helper: render the summary sub-tab ──
                def _render_tc_summary(report):
                    tc_summary_inner.clear()
                    with tc_summary_inner:
                        with ui.row().classes("w-full gap-4"):
                            with ui.card().classes("q-pa-md"):
                                ui.label("Total Nodes").classes("text-caption")
                                ui.label(str(report.total_nodes_analyzed)).classes(
                                    "text-h4 text-weight-bold"
                                )
                            with ui.card().classes("q-pa-md"):
                                ui.label("Nodes With Issues").classes("text-caption")
                                ui.label(str(report.nodes_with_issues)).classes(
                                    "text-h4 text-weight-bold text-negative"
                                )
                            with ui.card().classes("q-pa-md"):
                                ui.label("Missing Topic").classes("text-caption")
                                ui.label(str(report.nodes_missing_topic)).classes(
                                    "text-h4 text-weight-bold text-warning"
                                )

                        if report.node_type_breakdown:
                            ui.label("Node Type Breakdown").classes("text-subtitle1 q-mt-md")
                            type_rows = [
                                {"type": k, "count": v}
                                for k, v in sorted(report.node_type_breakdown.items())
                            ]
                            ui.table(
                                columns=[
                                    {"name": "type", "label": "Node Type", "field": "type", "sortable": True},
                                    {"name": "count", "label": "Count", "field": "count", "sortable": True},
                                ],
                                rows=type_rows,
                            ).classes("w-full q-mt-sm")

                        if report.issue_breakdown:
                            ui.label("Issue Breakdown").classes("text-subtitle1 q-mt-md")
                            issue_rows = [
                                {"issue": k, "count": v}
                                for k, v in sorted(report.issue_breakdown.items())
                            ]
                            ui.table(
                                columns=[
                                    {"name": "issue", "label": "Issue Type", "field": "issue", "sortable": True},
                                    {"name": "count", "label": "Count", "field": "count", "sortable": True},
                                ],
                                rows=issue_rows,
                            ).classes("w-full q-mt-sm")

                # ── Helper: render all-nodes table ──
                def _render_tc_all_nodes(report, type_filter="All", issue_filter="All"):
                    tc_all_table_container.clear()
                    analyses = report.analyses or []

                    if type_filter != "All":
                        analyses = [a for a in analyses if a.node_type == type_filter]
                    if issue_filter != "All":
                        analyses = [a for a in analyses if issue_filter in [i.value for i in a.issues]]

                    rows = []
                    for a in analyses:
                        issues_str = ", ".join(i.value for i in a.issues) if a.issues else "clean"
                        rows.append({
                            "key": a.node_key,
                            "node_type": a.node_type,
                            "parent": a.parent_snippet_key or "-",
                            "ef": a.execution_flow_name or a.execution_flow_key,
                            "topic": a.current_topic or "(missing)",
                            "business_summary": (a.business_summary[:80] + "...") if len(a.business_summary) > 80 else a.business_summary,
                            "issues": issues_str,
                            "suggestion": a.suggested_topic,
                            "confidence": f"{a.confidence:.0%}" if a.confidence else "-",
                        })

                    with tc_all_table_container:
                        if not rows:
                            ui.label("No nodes match the current filters.").classes("text-italic")
                            return

                        columns = [
                            {"name": "key", "label": "Node Key", "field": "key", "sortable": True},
                            {"name": "node_type", "label": "Type", "field": "node_type", "sortable": True},
                            {"name": "parent", "label": "Parent Snippet", "field": "parent", "sortable": True},
                            {"name": "ef", "label": "Execution Flow", "field": "ef", "sortable": True},
                            {"name": "topic", "label": "Current Topic", "field": "topic", "sortable": True},
                            {"name": "business_summary", "label": "Business Summary", "field": "business_summary"},
                            {"name": "issues", "label": "Issues", "field": "issues", "sortable": True},
                            {"name": "suggestion", "label": "Suggested Topic", "field": "suggestion"},
                            {"name": "confidence", "label": "Conf.", "field": "confidence", "sortable": True},
                        ]

                        tbl = ui.table(
                            columns=columns,
                            rows=rows,
                            row_key="key",
                            selection="multiple",
                            pagination={"rowsPerPage": 25},
                        ).classes("w-full")

                        tbl.on("selection", lambda e: _on_table_selection(e))

                        tbl.add_slot("body-cell-issues", r'''
                            <q-td :props="props">
                                <q-badge v-for="issue in props.value.split(', ')" :key="issue"
                                         :color="issue === 'clean' ? 'positive' :
                                                 (issue === 'duplicate' || issue === 'misaligned' || issue === 'topic_missing') ? 'negative' :
                                                 'warning'"
                                         :label="issue" class="q-mr-xs" />
                            </q-td>
                        ''')

                        for row in rows:
                            a = next((x for x in report.analyses if x.node_key == row["key"]), None)
                            if a and a.suggested_topic:
                                pass  # handled via slot above

                def _on_table_selection(e):
                    _tc_selected_keys.clear()
                    if hasattr(e, "selection"):
                        for item in e.selection:
                            _tc_selected_keys.add(item.get("key", ""))

                # ── Helper: render duplicate groups ──
                def _render_tc_dups(report):
                    tc_dups_container.clear()
                    with tc_dups_container:
                        if not report.duplicate_groups:
                            ui.label("No duplicate or similar topic groups found.").classes("text-italic")
                            return
                        for gid, keys in report.duplicate_groups.items():
                            with ui.expansion(f"Group: {gid} ({len(keys)} nodes)").classes("w-full"):
                                rows = []
                                for k in keys:
                                    a = next((x for x in report.analyses if x.node_key == k), None)
                                    if a:
                                        rows.append({
                                            "key": k,
                                            "type": a.node_type,
                                            "topic": a.current_topic or "(missing)",
                                            "business_summary": a.business_summary[:100],
                                            "suggestion": a.suggested_topic,
                                        })
                                if rows:
                                    ui.table(
                                        columns=[
                                            {"name": "key", "label": "Key", "field": "key"},
                                            {"name": "type", "label": "Type", "field": "type"},
                                            {"name": "topic", "label": "Current Topic", "field": "topic"},
                                            {"name": "business_summary", "label": "Business Summary", "field": "business_summary"},
                                            {"name": "suggestion", "label": "Suggested", "field": "suggestion"},
                                        ],
                                        rows=rows,
                                        row_key="key",
                                    ).classes("w-full")

                                    async def _apply_group(gkeys=keys):
                                        write_tool = await _get_write_tool()
                                        if not write_tool:
                                            return
                                        updates = []
                                        for gk in gkeys:
                                            ga = next((x for x in report.analyses if x.node_key == gk), None)
                                            if ga and ga.suggested_topic:
                                                updates.append({
                                                    "key": gk,
                                                    "label": ga.node_type,
                                                    "new_topic": ga.suggested_topic,
                                                })
                                        if updates:
                                            await write_tool.bulk_update_topics(updates)
                                            ui.notify(f"Applied {len(updates)} group suggestions", type="positive")
                                            _render_tc_audit(write_tool)

                                    ui.button(
                                        "Apply Group Suggestions", color="positive",
                                        on_click=_apply_group,
                                    ).props("outline size=sm").classes("q-mt-sm")

                # ── Helper: render journey chains ──
                def _render_tc_journey(report):
                    tc_journey_container.clear()
                    with tc_journey_container:
                        if not report.journey_chains:
                            ui.label("No journey chains found.").classes("text-italic")
                            return
                        for ef_key, topics in report.journey_chains.items():
                            with ui.expansion(f"Flow: {ef_key}").classes("w-full"):
                                chain_str = " → ".join(topics)
                                ui.label(f"Current: {chain_str}").classes("text-body2")

                                chain_analyses = [
                                    a for a in report.analyses
                                    if a.execution_flow_key == ef_key and a.chain_position is not None
                                ]
                                chain_analyses.sort(key=lambda a: a.chain_position or 0)

                                if chain_analyses:
                                    suggested_chain = " → ".join(
                                        a.suggested_topic or a.current_topic or "(missing)"
                                        for a in chain_analyses
                                    )
                                    ui.label(f"Suggested: {suggested_chain}").classes(
                                        "text-body2 text-positive q-mt-xs"
                                    )

                                    rows = []
                                    for a in chain_analyses:
                                        rows.append({
                                            "pos": a.chain_position,
                                            "key": a.node_key,
                                            "type": a.node_type,
                                            "current": a.current_topic or "(missing)",
                                            "suggested": a.suggested_topic,
                                        })
                                    ui.table(
                                        columns=[
                                            {"name": "pos", "label": "#", "field": "pos", "sortable": True},
                                            {"name": "key", "label": "Key", "field": "key"},
                                            {"name": "type", "label": "Type", "field": "type"},
                                            {"name": "current", "label": "Current Topic", "field": "current"},
                                            {"name": "suggested", "label": "Suggested Topic", "field": "suggested"},
                                        ],
                                        rows=rows,
                                        row_key="key",
                                    ).classes("w-full q-mt-sm")

                                    async def _apply_chain(analyses=chain_analyses):
                                        write_tool = await _get_write_tool()
                                        if not write_tool:
                                            return
                                        updates = []
                                        for ca in analyses:
                                            if ca.suggested_topic:
                                                updates.append({
                                                    "key": ca.node_key,
                                                    "label": ca.node_type,
                                                    "new_topic": ca.suggested_topic,
                                                })
                                        if updates:
                                            await write_tool.bulk_update_topics(
                                                updates, execution_flow_key=ef_key,
                                            )
                                            ui.notify(f"Applied {len(updates)} chain suggestions", type="positive")
                                            _render_tc_audit(write_tool)

                                    ui.button(
                                        "Apply Chain Suggestions", color="positive",
                                        on_click=_apply_chain,
                                    ).props("outline size=sm").classes("q-mt-sm")

                # ── Helper: render audit log ──
                def _render_tc_audit(write_tool):
                    tc_audit_container.clear()
                    with tc_audit_container:
                        changes = write_tool.change_log if write_tool else []
                        if not changes:
                            ui.label("No changes recorded yet.").classes("text-italic")
                            return
                        rows = []
                        for i, c in enumerate(changes):
                            rows.append({
                                "idx": i,
                                "key": c.node_key,
                                "label": c.node_label,
                                "old": c.old_topic,
                                "new": c.new_topic,
                                "by": c.changed_by,
                                "at": c.changed_at.strftime("%H:%M:%S"),
                                "undo": c.is_undo,
                            })
                        ui.table(
                            columns=[
                                {"name": "key", "label": "Node Key", "field": "key"},
                                {"name": "label", "label": "Label", "field": "label"},
                                {"name": "old", "label": "Old Topic", "field": "old"},
                                {"name": "new", "label": "New Topic", "field": "new"},
                                {"name": "by", "label": "Changed By", "field": "by"},
                                {"name": "at", "label": "Time", "field": "at"},
                                {"name": "undo", "label": "Is Undo", "field": "undo"},
                            ],
                            rows=rows,
                            row_key="idx",
                        ).classes("w-full")

                # ── Helper: get write tool ──
                async def _get_write_tool():
                    try:
                        reg = await _get_registry_async()
                        return reg.get("neo4j_write")
                    except (KeyError, RuntimeError):
                        ui.notify("Neo4jWriteTool not available", type="negative")
                        return None

                # ── Apply selected rows ──
                async def _on_apply_selected():
                    if not _tc_selected_keys:
                        ui.notify("No rows selected", type="warning")
                        return
                    report = _tc_report.get("data")
                    if not report:
                        return
                    write_tool = await _get_write_tool()
                    if not write_tool:
                        return
                    updates = []
                    for a in report.analyses:
                        if a.node_key in _tc_selected_keys and a.suggested_topic:
                            updates.append({
                                "key": a.node_key,
                                "label": a.node_type,
                                "new_topic": a.suggested_topic,
                            })
                    if updates:
                        await write_tool.bulk_update_topics(updates)
                        ui.notify(f"Applied {len(updates)} suggestions", type="positive")
                        _render_tc_audit(write_tool)
                    else:
                        ui.notify("No applicable suggestions for selected rows", type="info")

                tc_apply_selected_btn.on_click(_on_apply_selected)

                # ── Filter change handlers ──
                def _on_filter_change():
                    report = _tc_report.get("data")
                    if report:
                        _render_tc_all_nodes(
                            report,
                            type_filter=tc_filter_type.value,
                            issue_filter=tc_filter_issue.value,
                        )

                tc_filter_type.on("update:model-value", lambda _: _on_filter_change())
                tc_filter_issue.on("update:model-value", lambda _: _on_filter_change())

                # ── Export buttons ──
                async def _on_export_json():
                    write_tool = await _get_write_tool()
                    if write_tool:
                        content = write_tool.export_audit_json()
                        ui.download(content.encode(), "topic_audit_log.json")

                async def _on_export_csv():
                    write_tool = await _get_write_tool()
                    if write_tool:
                        content = write_tool.export_audit_csv()
                        ui.download(content.encode(), "topic_audit_log.csv")

                async def _on_undo_all():
                    write_tool = await _get_write_tool()
                    if not write_tool:
                        return
                    non_undo = [c for c in write_tool.change_log if not c.is_undo]
                    for c in reversed(non_undo):
                        await write_tool.restore_topic(
                            c.node_key, c.node_label, c.old_topic,
                            execution_flow_key=c.execution_flow_key,
                        )
                    ui.notify(f"Reverted {len(non_undo)} changes", type="positive")
                    _render_tc_audit(write_tool)

                tc_export_json_btn.on_click(_on_export_json)
                tc_export_csv_btn.on_click(_on_export_csv)
                tc_undo_all_btn.on_click(_on_undo_all)

                # ── Shared state for background task + timer-based polling ──
                _tc_progress_state = {"pct": 0.0, "msg": "", "done": False, "error": ""}
                _tc_bg_report = {"data": None}

                # ── Main analyze handler ──
                async def _on_analyze_topics():
                    pid_str = tc_project_input.value
                    rid_str = tc_run_input.value
                    if not pid_str or not rid_str:
                        ui.notify("Please enter both Project ID and Run ID", type="warning")
                        return

                    try:
                        pid = int(pid_str)
                        rid = int(rid_str)
                    except ValueError:
                        ui.notify("Project ID and Run ID must be integers", type="negative")
                        return

                    tc_analyze_btn.disable()
                    tc_progress.set_visibility(True)
                    tc_progress.set_value(0)
                    tc_status_label.set_text("Starting analysis...")
                    tc_results_section.set_visibility(False)
                    _tc_progress_state.update(pct=0.0, msg="Starting analysis...", done=False, error="")
                    _tc_bg_report["data"] = None

                    try:
                        reg = await _get_registry_async()
                        neo4j_tool = reg.get("neo4j")
                    except (KeyError, RuntimeError) as exc:
                        ui.notify(f"Neo4j tool not available: {exc}", type="negative")
                        tc_analyze_btn.enable()
                        tc_progress.set_visibility(False)
                        return

                    from trustbot.agents.topic_convergence import TopicConvergenceAgent

                    agent = TopicConvergenceAgent(neo4j_tool)

                    def _progress_cb(pct, msg):
                        _tc_progress_state["pct"] = pct
                        _tc_progress_state["msg"] = msg

                    async def _run_analysis():
                        """Runs in background — no UI calls here, only state updates."""
                        logger.info("[TC-UI] Background analysis task STARTED for pid=%d rid=%d", pid, rid)
                        try:
                            report = await agent.analyze(
                                pid, rid, progress_callback=_progress_cb,
                            )
                            _tc_bg_report["data"] = report
                            _tc_progress_state["done"] = True
                            logger.info(
                                "[TC-UI] Background analysis task COMPLETED: %d nodes, %d issues",
                                report.total_nodes_analyzed, report.nodes_with_issues,
                            )
                        except Exception as exc:
                            logger.exception("[TC-UI] Background analysis task FAILED: %s", exc)
                            _tc_progress_state["error"] = str(exc)
                            _tc_progress_state["done"] = True

                    from nicegui import background_tasks
                    logger.info("[TC-UI] Launching background task...")
                    background_tasks.create(_run_analysis())

                    def _poll_progress():
                        tc_progress.set_value(_tc_progress_state["pct"])
                        tc_status_label.set_text(_tc_progress_state["msg"])

                        if not _tc_progress_state["done"]:
                            return

                        logger.info("[TC-UI] Poll detected done=True, rendering results...")
                        poll_timer.deactivate()

                        if _tc_progress_state["error"]:
                            ui.notify(
                                f"Analysis failed: {_tc_progress_state['error']}",
                                type="negative",
                            )
                            tc_analyze_btn.enable()
                            tc_progress.set_visibility(False)
                            return

                        report = _tc_bg_report["data"]
                        if report is None:
                            tc_analyze_btn.enable()
                            tc_progress.set_visibility(False)
                            return

                        _tc_report["data"] = report

                        node_types = sorted(set(a.node_type for a in report.analyses))
                        tc_filter_type.options = ["All"] + node_types
                        tc_filter_type.value = "All"

                        _render_tc_summary(report)
                        _render_tc_all_nodes(report)
                        _render_tc_dups(report)
                        _render_tc_journey(report)

                        try:
                            reg = _get_registry()
                            wt = reg.get("neo4j_write")
                        except (KeyError, RuntimeError):
                            wt = None
                        _render_tc_audit(wt)

                        tc_results_section.set_visibility(True)
                        tc_progress.set_value(1.0)
                        tc_status_label.set_text(
                            f"Analysis complete: {report.total_nodes_analyzed} nodes, "
                            f"{report.nodes_with_issues} with issues"
                        )
                        tc_analyze_btn.enable()
                        tabs.set_value(tab_topic_conv)
                        logger.info("[TC-UI] Results rendered successfully on Tab 7")

                    poll_timer = ui.timer(0.5, _poll_progress)
                    logger.info("[TC-UI] Handler returned — background task running, poll timer active")

                tc_analyze_btn.on_click(_on_analyze_topics)

            # ═══════════════════════════════════════════════════════════
            # Tab 8: Chonkie CodeChunker POC
            # ═══════════════════════════════════════════════════════════
            with ui.tab_panel(tab_chonkie):
                ui.markdown(
                    "### Chonkie CodeChunker POC\n"
                    "Compare **3 chunking approaches** side-by-side:\n"
                    "1. **Regex** -- current TrustBot chunker (pattern matching)\n"
                    "2. **Chonkie AST** -- tree-sitter based (165+ languages)\n"
                    "3. **Structural** -- scope-aware block-boundary parser "
                    "(for RPG, FOCUS, Natural where no AST exists)\n\n"
                    "Paste code below **or** pick a sample file, then click "
                    "**Run Comparison**."
                )

                _CHONKIE_SAMPLES: dict[str, Path] = {}
                _sample_root = Path(__file__).resolve().parents[1] / "sample_codebase"
                _sample_exts = {
                    ".py", ".js", ".ts", ".java", ".go", ".cs", ".kt", ".rs",
                    ".cpp", ".c", ".rb", ".pas", ".dpr", ".cbl", ".cob",
                    ".rpg", ".rpgle", ".foc", ".nat",
                }
                if _sample_root.exists():
                    for _sf in sorted(_sample_root.rglob("*")):
                        if _sf.is_file() and _sf.suffix.lower() in _sample_exts:
                            _label = str(_sf.relative_to(_sample_root)).replace("\\", "/")
                            _CHONKIE_SAMPLES[_label] = _sf

                with ui.row().classes("w-full gap-4 items-end"):
                    chonkie_lang_select = ui.select(
                        ["auto", "python", "javascript", "typescript", "java",
                         "go", "csharp", "kotlin", "rust", "cpp", "c", "ruby",
                         "pascal (Delphi)", "cobol", "scala", "swift", "php",
                         "rpg", "focus", "natural"],
                        value="python",
                        label="Language",
                    ).classes("w-48")
                    chonkie_chunk_size = ui.number(
                        label="Chunk size (chars)", value=2048, min=128, max=16384,
                    ).classes("w-40")
                    chonkie_sample_select = ui.select(
                        {k: k for k in _CHONKIE_SAMPLES} if _CHONKIE_SAMPLES else {"": "(no samples found)"},
                        value="",
                        label="Load sample file",
                    ).classes("w-64")
                    chonkie_run_btn = ui.button(
                        "Run Comparison", color="primary",
                    )

                chonkie_code_input = ui.textarea(
                    label="Paste source code here",
                    placeholder="def hello():\n    print('world')\n\nclass Foo:\n    ...",
                ).classes("w-full").props("rows=14 outlined")

                def _on_sample_change(e):
                    key = e.value if hasattr(e, "value") else e
                    if key and key in _CHONKIE_SAMPLES:
                        try:
                            content = _CHONKIE_SAMPLES[key].read_text(
                                encoding="utf-8", errors="replace",
                            )
                            chonkie_code_input.set_value(content)
                            ext = _CHONKIE_SAMPLES[key].suffix
                            from trustbot.indexing.chunker import get_language_for_ext
                            detected_lang = get_language_for_ext(ext)
                            _ui_name_map = {
                                "delphi": "pascal (Delphi)",
                            }
                            ui_name = _ui_name_map.get(detected_lang, detected_lang)
                            chonkie_lang_select.set_value(ui_name if ui_name != "unknown" else "auto")
                        except Exception:
                            pass

                chonkie_sample_select.on_value_change(_on_sample_change)

                chonkie_status = ui.markdown("")

                with ui.row().classes("w-full gap-4"):
                    with ui.column().classes("flex-1"):
                        ui.label("1. Regex Chunker").classes(
                            "text-lg font-bold text-blue-600"
                        )
                        regex_summary = ui.markdown("")
                        regex_chunks_container = ui.column().classes("w-full gap-2")
                    with ui.column().classes("flex-1"):
                        ui.label("2. Chonkie AST Chunker").classes(
                            "text-lg font-bold text-green-600"
                        )
                        chonkie_summary = ui.markdown("")
                        chonkie_chunks_container = ui.column().classes("w-full gap-2")
                    with ui.column().classes("flex-1"):
                        ui.label("3. Structural Chunker").classes(
                            "text-lg font-bold text-purple-600"
                        )
                        ui.markdown(
                            "*Scope-aware block parser for RPG, FOCUS, Natural*"
                        )
                        structural_summary = ui.markdown("")
                        structural_chunks_container = ui.column().classes("w-full gap-2")

                from trustbot.indexing.structural_chunker import get_supported_languages as _get_struct_langs
                _STRUCTURAL_LANGS = set(_get_struct_langs())

                async def _on_run_chonkie_comparison():
                    code = (chonkie_code_input.value or "").strip()
                    if not code:
                        chonkie_status.set_content(
                            "**Please paste some code or select a sample file.**"
                        )
                        return

                    chonkie_run_btn.disable()
                    chonkie_status.set_content("*Running comparison...*")
                    regex_chunks_container.clear()
                    chonkie_chunks_container.clear()
                    structural_chunks_container.clear()

                    ui_language = chonkie_lang_select.value or "python"
                    chunk_size = int(chonkie_chunk_size.value or 2048)

                    _chonkie_lang_map = {
                        "pascal (Delphi)": "pascal",
                        "csharp": "c_sharp",
                    }
                    _ext_map = {
                        "python": ".py", "javascript": ".js",
                        "typescript": ".ts", "java": ".java",
                        "go": ".go", "csharp": ".cs", "kotlin": ".kt",
                        "rust": ".rs", "cpp": ".cpp", "c": ".c",
                        "ruby": ".rb", "auto": ".py",
                        "pascal (Delphi)": ".pas", "cobol": ".cbl",
                        "scala": ".scala", "swift": ".swift", "php": ".php",
                        "rpg": ".rpgle", "focus": ".foc", "natural": ".nat",
                    }
                    chonkie_language = _chonkie_lang_map.get(ui_language, ui_language)
                    ext = _ext_map.get(ui_language, ".py")
                    is_structural_lang = ui_language.lower() in _STRUCTURAL_LANGS

                    # ── 1. Regex chunker ─────────────────────────────
                    import tempfile
                    from trustbot.indexing.chunker import chunk_file as regex_chunk_file

                    with tempfile.TemporaryDirectory() as tmp:
                        tmp_path = Path(tmp)
                        tmp_file = tmp_path / f"sample{ext}"
                        tmp_file.write_text(code, encoding="utf-8")
                        regex_results = regex_chunk_file(tmp_file, tmp_path)

                    # ── 2. Chonkie AST chunker ───────────────────────
                    # Check tree-sitter support before attempting Chonkie
                    _no_treesitter = {
                        "rpg", "rpgle", "focus", "natural", "auto",
                    }
                    chonkie_results = []
                    chonkie_error = ""
                    if chonkie_language.lower() in _no_treesitter:
                        chonkie_error = (
                            f"No tree-sitter grammar for `{ui_language}`. "
                            f"Use the **Structural Chunker** (column 3) instead."
                        )
                    else:
                        try:
                            from chonkie import CodeChunker

                            chunker = CodeChunker(
                                language=chonkie_language,
                                tokenizer="character",
                                chunk_size=chunk_size,
                                include_nodes=False,
                            )
                            chonkie_results = chunker.chunk(code)
                        except Exception as exc:
                            logger.exception("Chonkie chunking failed")
                            chonkie_error = str(exc)

                    # ── 3. Structural block chunker ──────────────────
                    structural_results = []
                    structural_error = ""
                    try:
                        from trustbot.indexing.structural_chunker import (
                            structural_chunk,
                            get_supported_languages,
                        )
                        structural_results = structural_chunk(
                            code, ui_language, chunk_size,
                        )
                    except Exception as exc:
                        logger.exception("Structural chunking failed")
                        structural_error = str(exc)

                    # ── Render regex results ──────────────────────────
                    regex_summary.set_content(
                        f"**Chunks:** {len(regex_results)} | "
                        f"**Method:** Regex pattern matching"
                    )
                    for idx, rc in enumerate(regex_results, 1):
                        with regex_chunks_container:
                            with ui.card().classes("w-full"):
                                ui.label(
                                    f"Chunk {idx}: {rc.function_name}"
                                    f" (lines {rc.line_start}-{rc.line_end})"
                                ).classes("font-bold text-sm text-blue-800")
                                with ui.expansion(
                                    f"Preview ({rc.line_end - rc.line_start + 1} lines)",
                                    icon="code",
                                ).classes("w-full"):
                                    ui.code(rc.content).classes(
                                        "w-full max-h-64 overflow-auto text-xs"
                                    )

                    # ── Render Chonkie results ────────────────────────
                    if chonkie_error:
                        chonkie_summary.set_content(
                            f"**Error:** {chonkie_error}"
                        )
                    else:
                        total_tokens = sum(c.token_count for c in chonkie_results)
                        chonkie_summary.set_content(
                            f"**Chunks:** {len(chonkie_results)} | "
                            f"**Total tokens:** {total_tokens} | "
                            f"**Method:** AST (tree-sitter)"
                        )
                        for idx, cc in enumerate(chonkie_results, 1):
                            with chonkie_chunks_container:
                                with ui.card().classes("w-full"):
                                    ui.label(
                                        f"Chunk {idx}: {cc.token_count} tokens "
                                        f"(chars {cc.start_index}-{cc.end_index})"
                                    ).classes("font-bold text-sm text-green-800")
                                    with ui.expansion(
                                        f"Preview ({cc.token_count} tokens)",
                                        icon="code",
                                    ).classes("w-full"):
                                        ui.code(cc.text).classes(
                                            "w-full max-h-64 overflow-auto text-xs"
                                        )

                    # ── Render structural results ─────────────────────
                    if structural_error:
                        structural_summary.set_content(
                            f"**Error:** {structural_error}"
                        )
                    elif not structural_results:
                        structural_summary.set_content(
                            "**No blocks found** -- language may not have "
                            "structural rules defined yet."
                        )
                    else:
                        total_sc = sum(s.token_count for s in structural_results)
                        structural_summary.set_content(
                            f"**Chunks:** {len(structural_results)} | "
                            f"**Total chars:** {total_sc} | "
                            f"**Method:** Block-boundary scope parsing"
                        )
                        for idx, sc in enumerate(structural_results, 1):
                            with structural_chunks_container:
                                with ui.card().classes("w-full"):
                                    ui.label(
                                        f"Chunk {idx}: [{sc.block_type}] "
                                        f"{sc.block_name} "
                                        f"(lines {sc.line_start}-{sc.line_end})"
                                    ).classes("font-bold text-sm text-purple-800")
                                    with ui.expansion(
                                        f"Preview ({sc.token_count} chars)",
                                        icon="code",
                                    ).classes("w-full"):
                                        ui.code(sc.text).classes(
                                            "w-full max-h-64 overflow-auto text-xs"
                                        )

                    # ── Comparison summary table ──────────────────────
                    n_regex = len(regex_results)
                    n_chonkie = len(chonkie_results) if not chonkie_error else 0
                    n_structural = len(structural_results)

                    avg_r = (
                        sum(len(r.content) for r in regex_results) // max(n_regex, 1)
                        if n_regex else "N/A"
                    )
                    avg_c = (
                        sum(c.token_count for c in chonkie_results) // max(n_chonkie, 1)
                        if n_chonkie else "N/A"
                    )
                    avg_s = (
                        sum(s.token_count for s in structural_results) // max(n_structural, 1)
                        if n_structural else "N/A"
                    )

                    has_ast = not chonkie_error and n_chonkie > 0
                    best_for_lang = (
                        "**Structural** (no AST grammar for this language)"
                        if is_structural_lang else
                        "**Chonkie AST** (tree-sitter grammar available)"
                        if has_ast else "**Regex** (fallback)"
                    )

                    summary_lines = [
                        "---",
                        "### Comparison Summary",
                        "",
                        "| Metric | Regex | Chonkie (AST) | Structural |",
                        "| --- | --- | --- | --- |",
                        f"| Chunks produced | {n_regex} "
                        f"| {n_chonkie if not chonkie_error else 'N/A'} "
                        f"| {n_structural} |",
                        f"| Avg chunk size (chars) | {avg_r} | {avg_c} | {avg_s} |",
                        "| Parsing method | Regex | tree-sitter AST "
                        "| Block-boundary scope |",
                        "| Language support | ~15 (manual) | 165+ (auto) "
                        "| RPG, FOCUS, Natural |",
                        "| Structural awareness | No | Full AST "
                        "| Block-level (open/close) |",
                        "| Token counting | No | Yes | Yes (chars) |",
                        "| Handles nesting | No | Yes | Yes |",
                        "",
                        f"**Recommended for `{ui_language}`:** {best_for_lang}",
                    ]
                    chonkie_status.set_content("\n".join(summary_lines))
                    chonkie_run_btn.enable()

                chonkie_run_btn.on_click(_on_run_chonkie_comparison)
