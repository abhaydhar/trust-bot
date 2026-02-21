"""Enhanced Gradio-based web UI for TrustBot with 3-agent validation pipeline."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
from pathlib import Path

import gradio as gr

from trustbot.agent.orchestrator import AgentOrchestrator
from trustbot.agents.pipeline import ValidationPipeline
from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.models.agentic import VerificationResult
from trustbot.models.validation import EdgeVerdict, NodeVerdict, ProjectValidationReport
from trustbot.tools.base import ToolRegistry

logger = logging.getLogger("trustbot.ui")


def create_ui(registry: ToolRegistry, code_index: CodeIndex | None = None) -> gr.Blocks:
    orchestrator = AgentOrchestrator(registry)
    git_index = None

    # Auto-load existing git index if the DB file exists from a previous session
    git_index_path = settings.codebase_root / ".trustbot_git_index.db"
    if git_index_path.exists():
        try:
            git_index = CodeIndex(db_path=git_index_path)
            logger.info("Auto-loaded existing git index from %s", git_index_path)
        except Exception as e:
            logger.warning("Could not auto-load git index: %s", e)

    active_index = git_index or code_index

    pipeline: ValidationPipeline | None = None
    try:
        pipeline = ValidationPipeline(
            neo4j_tool=registry.get("neo4j"),
            code_index=active_index,
        )
    except KeyError:
        logger.warning("ValidationPipeline not available (missing neo4j tool)")

    main_loop = asyncio.get_event_loop()

    def _run_async(coro):
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        return future.result()

    # Shared progress state for streaming progress to UI
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

    # ── Async handlers ──────────────────────────────────────────────────

    async def clone_and_index_repo(git_url: str, branch: str, progress=gr.Progress()):
        """Clone a git repo and build code index."""
        nonlocal git_index
        if not git_url.strip():
            return "Please enter a Git repository URL."
        try:
            progress(0, desc="Cloning repository...")
            from trustbot.indexing.git_indexer import GitCodeIndexer
            indexer = GitCodeIndexer()
            progress(0.2, desc="Downloading code...")
            result = await indexer.clone_and_index(
                git_url.strip(), branch.strip() or "main",
                progress_callback=lambda p, d: progress(0.2 + 0.6 * p, desc=d),
            )
            progress(0.9, desc="Finalizing...")
            git_index_path = settings.codebase_root / ".trustbot_git_index.db"
            git_index = CodeIndex(db_path=git_index_path)

            # Automatically wire the new index into the pipeline
            if pipeline:
                pipeline.set_code_index(git_index)

            progress(1.0, desc="Done!")
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

    async def index_local_folder(folder_path: str, progress=gr.Progress()):
        """Index code directly from a local folder (no git clone)."""
        nonlocal git_index

        folder_path = (folder_path or "").strip()
        if not folder_path:
            return "Please enter a folder path."

        folder = Path(folder_path)
        if not folder.exists():
            return f"Folder does not exist: `{folder}`"
        if not folder.is_dir():
            return f"Path is not a directory: `{folder}`"

        try:
            progress(0.05, desc="Scanning local folder...")

            from trustbot.indexing.chunker import chunk_codebase
            from trustbot.indexing.call_graph_builder import build_call_graph_from_chunks

            chunks = await asyncio.to_thread(chunk_codebase, folder)

            progress(0.35, desc=f"Found {len(chunks)} code chunks, building index...")

            git_index_path = settings.codebase_root / ".trustbot_git_index.db"
            code_idx = CodeIndex(db_path=git_index_path)
            code_idx.build(codebase_root=folder)

            function_count = len([c for c in chunks if c.function_name])
            progress(0.55, desc=f"Building call graph from {function_count} functions...")

            edges = await asyncio.to_thread(build_call_graph_from_chunks, chunks)

            edge_tuples = [(e.from_chunk, e.to_chunk, e.confidence) for e in edges]
            code_idx.store_edges(edge_tuples)
            code_idx.close()

            progress(0.9, desc="Finalizing...")

            git_index = CodeIndex(db_path=git_index_path)
            if pipeline:
                pipeline.set_code_index(git_index)

            files_count = len(set(c.file_path for c in chunks))
            progress(1.0, desc="Done!")

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

    async def validate_all_flows(project_id_str: str, run_id_str: str):
        """3-agent validation across all flows in a project."""
        _empty = ("", "", "", "", "")
        if not project_id_str.strip() or not run_id_str.strip():
            return ("Please enter both Project ID and Run ID.",) + _empty
        try:
            project_id = int(project_id_str.strip())
            run_id = int(run_id_str.strip())
        except ValueError:
            return ("Project ID and Run ID must be integers.",) + _empty

        if not pipeline:
            return ("Pipeline not available. Neo4j tool is missing.",) + _empty
        if not pipeline.has_index:
            return (
                "**No codebase indexed.** Please go to the **Code Indexer** tab first, "
                "clone the repository, and then return here to validate.",
            ) + _empty

        try:
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

                result, report_md, neo4j_g, index_g = await pipeline.validate_flow(
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

            summary_md = _format_3agent_summary(project_id, run_id, all_results)
            report_md = _format_3agent_report(project_id, run_id, all_results)
            calltree = _build_mermaid_panel(all_results)
            agent1_md = _format_agent_output("Agent 1 (Neo4j)", all_results, "neo4j_graph")
            agent2_md = _format_agent_output("Agent 2 (Index)", all_results, "index_graph")
            raw_json = json.dumps(
                [_result_to_dict(r) for r in all_results],
                indent=2, default=str,
            )

            _set_progress(1.0, "Validation complete!", done=True)
            return summary_md, report_md, calltree, agent1_md, agent2_md, raw_json

        except ValueError as e:
            _set_progress(1.0, f"Error: {e}", done=True)
            return f"Error: {e}", "", "", "", "", ""
        except Exception as e:
            logger.exception("Validation failed")
            _set_progress(1.0, f"Error: {e}", done=True)
            return f"Unexpected error: {e}", "", "", "", "", ""

    async def handle_chat(message: str):
        if not message.strip():
            return "Please enter a question."
        try:
            return await orchestrator.chat(message)
        except Exception as e:
            logger.exception("Chat failed")
            return f"Error: {e}"

    async def run_reindex(force: bool):
        try:
            index_tool = registry.get("index")
        except KeyError:
            return "Index tool not available (ChromaDB not loaded)"
        try:
            stats = await index_tool.call("reindex", force=force)
            return (
                f"Indexing complete.\n"
                f"Files processed: {stats['files']}\n"
                f"Chunks created: {stats['chunks']}\n"
                f"New chunks: {stats['new']}\n"
                f"Skipped (unchanged): {stats['skipped']}"
            )
        except Exception as e:
            return f"Indexing failed: {e}"

    async def get_status():
        try:
            index_tool = registry.get("index")
        except KeyError:
            return "Index tool not available (ChromaDB not loaded)"
        try:
            status = await index_tool.call("get_index_status")
            return json.dumps(status, indent=2)
        except Exception as e:
            return f"Error: {e}"

    async def get_chunk_data():
        try:
            from trustbot.indexing.chunk_visualizer import ChunkVisualizer
            active_index = git_index if git_index else code_index
            viz = ChunkVisualizer(active_index)
            return await viz.get_graph_data()
        except Exception as e:
            logger.exception("Chunk visualization failed")
            return {"nodes": [], "edges": []}

    # ── Gradio Layout ───────────────────────────────────────────────────

    app = gr.Blocks(title="TrustBot")

    with app:
        gr.Markdown("# TrustBot\n*3-Agent call graph validation: Neo4j vs Indexed Codebase*")

        with gr.Tabs():

            # ─── Tab 1: Code Indexer (first!) ───────────────────────────
            with gr.Tab("1. Code Indexer"):
                gr.Markdown(
                    "### Step 1: Index Your Codebase\n"
                    "Clone a git repository **or** select a local folder to build a "
                    "code index. This index is used by **Agent 2** during validation "
                    "to independently reconstruct the call graph from source code.\n\n"
                    "After indexing, switch to the **Validate** tab."
                )

                source_radio = gr.Radio(
                    choices=["Git Repository", "Local Folder"],
                    value="Git Repository",
                    label="Source",
                )

                git_row = gr.Row(visible=True)
                with git_row:
                    git_url_input = gr.Textbox(
                        label="Git Repository URL",
                        placeholder="https://github.com/username/repo.git",
                        scale=3,
                    )
                    branch_input = gr.Textbox(
                        label="Branch", placeholder="main", value="main", scale=1,
                    )

                local_row = gr.Row(visible=False)
                with local_row:
                    folder_path_input = gr.File(
                        label="Folder Path",
                        file_count="directory",
                        type="filepath",
                        interactive=True,
                        # default_path not supported in Gradio 6
                    )

                def _toggle_source(source):
                    is_local = source == "Local Folder"
                    return gr.update(visible=not is_local), gr.update(visible=is_local)

                source_radio.change(
                    fn=_toggle_source,
                    inputs=[source_radio],
                    outputs=[git_row, local_row],
                )

                index_repo_btn = gr.Button("Index Codebase", variant="primary")
                index_status = gr.Markdown(label="Status")

                def _do_index(source, git_url, branch, folder_path):
                    try:
                        if source == "Local Folder":
                            result = _run_async(index_local_folder(folder_path))
                        else:
                            result = _run_async(clone_and_index_repo(git_url, branch))
                    except Exception as e:
                        result = f"Error: {e}"
                    return gr.update(interactive=True), result

                index_repo_btn.click(
                    fn=lambda: gr.update(interactive=False),
                    outputs=[index_repo_btn],
                ).then(
                    fn=_do_index,
                    inputs=[source_radio, git_url_input, branch_input, folder_path_input],
                    outputs=[index_repo_btn, index_status],
                )

            # ─── Tab 2: Validate (3-agent pipeline) ────────────────────
            with gr.Tab("2. Validate"):
                gr.Markdown(
                    "### Step 2: Validate Execution Flows\n"
                    "Enter the Project ID and Run ID from Neo4j. "
                    "The 3-agent pipeline will:\n\n"
                    "1. **Agent 1** -- Fetch call graphs from Neo4j and identify ROOT snippets\n"
                    "2. **Agent 2** -- Build call graphs from the indexed codebase starting at ROOT\n"
                    "3. **Agent 3** -- Compare both graphs, classify edges, compute trust scores"
                )

                with gr.Row():
                    project_id_input = gr.Textbox(label="Project ID", placeholder="e.g. 3151", scale=2)
                    run_id_input = gr.Textbox(label="Run ID", placeholder="e.g. 4912", scale=2)
                    validate_btn = gr.Button("Validate All Flows", variant="primary", scale=1)

                progress_html = gr.HTML(value="", visible=True)
                summary_output = gr.Markdown(label="Summary")

                with gr.Accordion("Detailed Report", open=False):
                    report_output = gr.Markdown(label="Structured Report")

                with gr.Accordion("Call Tree Diagrams", open=False):
                    calltree_html = gr.HTML(label="Mermaid Call Trees")

                with gr.Accordion("Agent 1 Output (Neo4j Call Graph)", open=False):
                    agent1_output = gr.Markdown(label="Agent 1 — Neo4j edges")

                with gr.Accordion("Agent 2 Output (Indexed Codebase Call Graph)", open=False):
                    agent2_output = gr.Markdown(label="Agent 2 — Index edges")

                with gr.Accordion("Raw JSON", open=False):
                    json_output = gr.Code(label="Report JSON", language="json")

                def _build_progress_bar(pct: float, step: str) -> str:
                    width = max(0, min(100, int(pct * 100)))
                    if width >= 100:
                        color_from, color_to = "#2196F3", "#42A5F5"
                    elif "Agent 1" in step or "agent1" in step.lower():
                        color_from, color_to = "#FF9800", "#FFB74D"
                    elif "Agent 2" in step or "agent2" in step.lower():
                        color_from, color_to = "#9C27B0", "#BA68C8"
                    elif "Agent 3" in step or "agent3" in step.lower():
                        color_from, color_to = "#4CAF50", "#66BB6A"
                    else:
                        color_from, color_to = "#4CAF50", "#66BB6A"
                    return (
                        f'<div style="margin:12px 0;">'
                        f'<div style="display:flex;justify-content:space-between;margin-bottom:4px;">'
                        f'<span style="font-weight:600;font-size:14px;">{step}</span>'
                        f'<span style="font-weight:700;font-size:14px;">{width}%</span>'
                        f'</div>'
                        f'<div style="width:100%;background:#e0e0e0;border-radius:8px;height:22px;overflow:hidden;">'
                        f'<div style="width:{width}%;background:linear-gradient(90deg,{color_from},{color_to});'
                        f'height:100%;border-radius:8px;transition:width 0.4s ease;"></div>'
                        f'</div></div>'
                    )

                def _do_validate(p, r):
                    """Generator: stream progress updates, then final results."""
                    _set_progress(0.0, "Initializing...", done=False)
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    future = pool.submit(lambda: _run_async(validate_all_flows(p, r)))

                    while not future.done():
                        state = _get_progress()
                        bar = _build_progress_bar(state["pct"], state["step"])
                        yield (
                            gr.update(interactive=False),
                            bar,
                            gr.update(), gr.update(), gr.update(),
                            gr.update(), gr.update(), gr.update(),
                        )
                        time.sleep(0.3)

                    try:
                        result = future.result()
                    except Exception as e:
                        result = (f"Error: {e}", "", "", "", "", "")

                    done_bar = _build_progress_bar(1.0, "Validation complete!")
                    yield (
                        gr.update(interactive=True),
                        done_bar,
                        result[0], result[1], result[2],
                        result[3], result[4], result[5],
                    )
                    pool.shutdown(wait=False)

                validate_btn.click(
                    fn=_do_validate,
                    inputs=[project_id_input, run_id_input],
                    outputs=[
                        validate_btn, progress_html,
                        summary_output, report_output, calltree_html,
                        agent1_output, agent2_output,
                        json_output,
                    ],
                )

            # ─── Tab 3: Chunk Visualizer ────────────────────────────────
            with gr.Tab("3. Chunk Visualizer"):
                gr.Markdown(
                    "### Code Chunk Graph\n"
                    "Browse all indexed code chunks and their call relationships.\n"
                    "Index a repository first using the **Code Indexer** tab."
                )
                all_nodes_state = gr.State([])
                all_edges_state = gr.State([])
                node_page_state = gr.State(1)
                edge_page_state = gr.State(1)

                with gr.Row():
                    refresh_btn = gr.Button("Refresh Visualization", variant="primary", scale=2)
                    page_size_dd = gr.Dropdown(
                        choices=["25", "50", "100", "200"],
                        value="50", label="Rows per page", interactive=True, scale=1,
                    )
                chunk_stats = gr.Markdown(value="Click **Refresh** after indexing a repository.")

                with gr.Tabs():
                    with gr.Tab("Functions"):
                        node_page_info = gr.Markdown(value="")
                        chunk_table = gr.Dataframe(
                            headers=["#", "Function", "File", "Language", "Type"],
                            datatype=["str", "str", "str", "str", "str"],
                            label="Indexed Functions", interactive=False, wrap=True,
                        )
                        with gr.Row():
                            node_prev_btn = gr.Button("Previous", size="sm")
                            node_next_btn = gr.Button("Next", size="sm")
                    with gr.Tab("Call Relationships"):
                        edge_page_info = gr.Markdown(value="")
                        edge_table = gr.Dataframe(
                            headers=["#", "Caller", "Callee", "Confidence"],
                            datatype=["str", "str", "str", "str"],
                            label="Call Graph Edges", interactive=False, wrap=True,
                        )
                        with gr.Row():
                            edge_prev_btn = gr.Button("Previous", size="sm")
                            edge_next_btn = gr.Button("Next", size="sm")

                def _paginate(rows, page, page_size):
                    total = len(rows)
                    total_pages = max(1, (total + page_size - 1) // page_size)
                    page = max(1, min(page, total_pages))
                    start = (page - 1) * page_size
                    end = min(start + page_size, total)
                    return rows[start:end], page, total_pages

                def _node_rows(nodes):
                    return [
                        [str(i), str(n.get("name", "")),
                         str(n.get("file", "")).replace("\\", "/"),
                         str(n.get("language", "")), str(n.get("type", "function"))]
                        for i, n in enumerate(nodes, 1)
                    ]

                def _edge_rows(edges):
                    return [
                        [str(i), _short_chunk_id(str(e.get("from", ""))),
                         _short_chunk_id(str(e.get("to", ""))),
                         str(e.get("confidence", ""))]
                        for i, e in enumerate(edges, 1)
                    ]

                def render_chunk_viz(page_size_str="50"):
                    try:
                        ps = int(page_size_str or "50")
                        data = _run_async(get_chunk_data())
                        nodes = data.get("nodes", [])
                        edges = data.get("edges", [])
                        nrows = _node_rows(nodes)
                        erows = _edge_rows(edges)
                        ns, np_, nt = _paginate(nrows, 1, ps)
                        es, ep_, et = _paginate(erows, 1, ps)
                        return (
                            nodes, edges, 1, 1,
                            f"**Total Functions**: {len(nodes)} | **Total Call Relationships**: {len(edges)}",
                            f"Page **{np_}** of **{nt}** ({len(nodes)} functions)", ns,
                            f"Page **{ep_}** of **{et}** ({len(edges)} edges)", es,
                        )
                    except Exception as e:
                        logger.exception("Chunk visualization failed")
                        return [], [], 1, 1, f"Error: {e}", "", [], "", []

                def _do_refresh_viz(ps):
                    try:
                        result = render_chunk_viz(ps)
                    except Exception as e:
                        result = ([], [], 1, 1, f"Error: {e}", "", [], "", [])
                    return (gr.update(interactive=True), *result)

                refresh_btn.click(
                    fn=lambda: gr.update(interactive=False), outputs=[refresh_btn],
                ).then(
                    fn=_do_refresh_viz, inputs=[page_size_dd],
                    outputs=[refresh_btn, all_nodes_state, all_edges_state,
                             node_page_state, edge_page_state, chunk_stats,
                             node_page_info, chunk_table, edge_page_info, edge_table],
                )

                def go_node_page(nodes, cp, delta, ps_str):
                    ps = int(ps_str)
                    sl, pg, tp = _paginate(_node_rows(nodes), cp + delta, ps)
                    return pg, f"Page **{pg}** of **{tp}** ({len(nodes)} functions)", sl

                def go_edge_page(edges, cp, delta, ps_str):
                    ps = int(ps_str)
                    sl, pg, tp = _paginate(_edge_rows(edges), cp + delta, ps)
                    return pg, f"Page **{pg}** of **{tp}** ({len(edges)} edges)", sl

                node_prev_btn.click(
                    fn=lambda n, p, ps: go_node_page(n, p, -1, ps),
                    inputs=[all_nodes_state, node_page_state, page_size_dd],
                    outputs=[node_page_state, node_page_info, chunk_table],
                )
                node_next_btn.click(
                    fn=lambda n, p, ps: go_node_page(n, p, 1, ps),
                    inputs=[all_nodes_state, node_page_state, page_size_dd],
                    outputs=[node_page_state, node_page_info, chunk_table],
                )
                edge_prev_btn.click(
                    fn=lambda e, p, ps: go_edge_page(e, p, -1, ps),
                    inputs=[all_edges_state, edge_page_state, page_size_dd],
                    outputs=[edge_page_state, edge_page_info, edge_table],
                )
                edge_next_btn.click(
                    fn=lambda e, p, ps: go_edge_page(e, p, 1, ps),
                    inputs=[all_edges_state, edge_page_state, page_size_dd],
                    outputs=[edge_page_state, edge_page_info, edge_table],
                )

            # ─── Tab 4: Chat ────────────────────────────────────────────
            with gr.Tab("4. Chat"):
                gr.Markdown("Ask TrustBot questions about execution flows, code, or the knowledge graph.")
                chat_input = gr.Textbox(label="Your Question", placeholder="Ask TrustBot...", lines=2)
                chat_btn = gr.Button("Send", variant="primary")
                chat_output = gr.Markdown(label="Response")

                def _do_chat(m):
                    try:
                        result = _run_async(handle_chat(m))
                    except Exception as e:
                        result = f"Error: {e}"
                    return gr.update(interactive=True), result

                chat_btn.click(
                    fn=lambda: gr.update(interactive=False), outputs=[chat_btn],
                ).then(fn=_do_chat, inputs=[chat_input], outputs=[chat_btn, chat_output])

            # ─── Tab 5: Index Management ────────────────────────────────
            with gr.Tab("5. Index Management"):
                gr.Markdown("### Codebase Index Management")
                with gr.Row():
                    index_btn = gr.Button("Incremental Re-index")
                    force_index_btn = gr.Button("Full Re-index", variant="secondary")
                    status_btn = gr.Button("Check Status")
                index_output = gr.Textbox(label="Result", lines=6, interactive=False)

                def _do_reindex(force, _):
                    try:
                        result = _run_async(run_reindex(force))
                    except Exception as e:
                        result = f"Error: {e}"
                    return (gr.update(interactive=True),) * 3 + (result,)

                def _do_status():
                    try:
                        result = _run_async(get_status())
                    except Exception as e:
                        result = f"Error: {e}"
                    return (gr.update(interactive=True),) * 3 + (result,)

                _idx_btns = [index_btn, force_index_btn, status_btn]
                _dis = lambda: (gr.update(interactive=False),) * 3

                index_btn.click(fn=_dis, outputs=_idx_btns).then(
                    fn=lambda: _do_reindex(False, None), outputs=[*_idx_btns, index_output])
                force_index_btn.click(fn=_dis, outputs=_idx_btns).then(
                    fn=lambda: _do_reindex(True, None), outputs=[*_idx_btns, index_output])
                status_btn.click(fn=_dis, outputs=_idx_btns).then(
                    fn=_do_status, outputs=[*_idx_btns, index_output])

    return app


# ── Report Formatting ───────────────────────────────────────────────────

def _format_3agent_summary(
    project_id: int, run_id: int, results: list[dict]
) -> str:
    """Markdown summary for the 3-agent validation."""
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
        f"## 3-Agent Validation Summary",
        f"**Project ID**: {project_id} | **Run ID**: {run_id} | **Flows**: {len(results)}",
        "",
        f"### Key Metrics",
        f"- **Average Trust Score**: {avg_trust:.0%}",
        f"- **Total Edges Analyzed**: {total_edges}",
        f"  - Confirmed: {total_confirmed}",
        f"  - Phantom (Neo4j only): {total_phantom}",
        f"  - Missing (Index only): {total_missing}",
        "",
    ]

    if needs_attention:
        lines.append(f"### Flows Requiring Attention ({len(needs_attention)})")
        for r in needs_attention[:10]:
            res = r["result"]
            lines.append(
                f"- **{r['flow_name']}** (`{r['flow_key'][:12]}...`): "
                f"trust {res.flow_trust_score:.0%}, "
                f"{len(res.phantom_edges)} phantom, {len(res.missing_edges)} missing"
            )
        lines.append("")

    return "\n".join(lines)


def _format_3agent_report(
    project_id: int, run_id: int, results: list[dict]
) -> str:
    """Detailed markdown report with Agent 1, Agent 2, and Agent 3 output per flow."""
    from trustbot.models.agentic import CallGraphOutput, normalize_file_path
    from trustbot.ui.call_tree import build_text_tree

    lines = [
        f"# 3-Agent Validation Report",
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
        lines.append(
            f"## Flow {idx+1}/{len(results)}: {flow_name}"
        )
        lines.append(
            f"**Key**: `{flow_key}` | "
            f"**Trust**: {res.flow_trust_score:.0%} | "
            f"**Neo4j edges**: {neo4j_edges} | **Index edges**: {index_edges}"
        )
        lines.append("")

        # ── Agent 1: Neo4j Call Graph ───────────────────────────────
        lines.append("### Agent 1 — Neo4j Call Graph")
        lines.append("")
        if neo4j_graph:
            lines.append(
                f"**Root**: `{neo4j_graph.root_function}` | "
                f"**Edges**: {len(neo4j_graph.edges)}"
            )
            lines.append("")
            # Call tree visualization
            if neo4j_graph.edges:
                tree = build_text_tree(neo4j_graph, "Neo4j")
                lines.append("**Call Tree:**")
                lines.append("")
                lines.append("```")
                lines.append(tree)
                lines.append("```")
                lines.append("")
                lines.append("**Edge Details:**")
                lines.append("")
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
                    lines.append(f"| ... | +{len(neo4j_graph.edges) - 40} more | | | | | |")
            else:
                lines.append("*No edges.*")
            lines.append("")
        else:
            lines.append("*Agent 1 data not available.*")
            lines.append("")

        # ── Agent 2: Indexed Codebase Call Graph ────────────────────
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
            # Call tree visualization
            if index_graph.edges:
                tree = build_text_tree(index_graph, "Index")
                lines.append("**Call Tree:**")
                lines.append("")
                lines.append("```")
                lines.append(tree)
                lines.append("```")
                lines.append("")
                lines.append("**Edge Details:**")
                lines.append("")
                lines.append("| # | Caller | Class | File | Callee | Class | File | Conf |")
                lines.append("|---|--------|-------|------|--------|-------|------|------|")
                for i, e in enumerate(index_graph.edges[:40], 1):
                    cr_file = normalize_file_path(e.caller_file) or "-"
                    ce_file = normalize_file_path(e.callee_file) or "-"
                    lines.append(
                        f"| {i} | `{e.caller}` | {e.caller_class or '-'} | {cr_file} "
                        f"| `{e.callee}` | {e.callee_class or '-'} | {ce_file} "
                        f"| {e.confidence:.2f} |"
                    )
                if len(index_graph.edges) > 40:
                    lines.append(f"| ... | +{len(index_graph.edges) - 40} more | | | | | | |")
            else:
                lines.append("**No edges found.** The root function may not have outgoing calls in the indexed codebase.")
            lines.append("")
            if index_graph.unresolved_callees:
                lines.append(f"**Unresolved callees** ({len(index_graph.unresolved_callees)}): "
                             + ", ".join(f"`{u}`" for u in index_graph.unresolved_callees[:15]))
                lines.append("")
        else:
            lines.append("*Agent 2 data not available.*")
            lines.append("")

        # ── Agent 3: Comparison ─────────────────────────────────────
        lines.append("### Agent 3 — Comparison Results")
        lines.append("")

        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Trust Score | {res.flow_trust_score:.2%} |")
        lines.append(f"| Confirmed | {len(res.confirmed_edges)} |")
        lines.append(f"| -- Full match (name+class+file) | {meta.get('match_full', '-')} |")
        lines.append(f"| -- Name+file match | {meta.get('match_name_file', '-')} |")
        lines.append(f"| -- Name-only match | {meta.get('match_name_only', '-')} |")
        lines.append(f"| Phantom (Neo4j only) | {len(res.phantom_edges)} |")
        lines.append(f"| Missing (Index only) | {len(res.missing_edges)} |")
        lines.append("")

        if res.confirmed_edges:
            lines.append("**Confirmed Edges**")
            lines.append("")
            lines.append("| # | Caller | Callee | Trust | Match Type |")
            lines.append("|---|--------|--------|-------|------------|")
            for i, e in enumerate(res.confirmed_edges[:30], 1):
                lines.append(f"| {i} | `{e.caller}` | `{e.callee}` | {e.trust_score:.2f} | {e.details} |")
            if len(res.confirmed_edges) > 30:
                lines.append(f"| ... | +{len(res.confirmed_edges) - 30} more | | | |")
            lines.append("")

        if res.phantom_edges:
            lines.append("**Phantom Edges** (in Neo4j but NOT in indexed codebase)")
            lines.append("")
            lines.append("| # | Caller | Callee | Details |")
            lines.append("|---|--------|--------|---------|")
            for i, e in enumerate(res.phantom_edges[:30], 1):
                lines.append(f"| {i} | `{e.caller}` | `{e.callee}` | {e.details} |")
            if len(res.phantom_edges) > 30:
                lines.append(f"| ... | +{len(res.phantom_edges) - 30} more | | |")
            lines.append("")

        if res.missing_edges:
            lines.append("**Missing Edges** (in indexed codebase but NOT in Neo4j)")
            lines.append("")
            lines.append("| # | Caller | Callee | Details |")
            lines.append("|---|--------|--------|---------|")
            for i, e in enumerate(res.missing_edges[:30], 1):
                lines.append(f"| {i} | `{e.caller}` | `{e.callee}` | {e.details} |")
            if len(res.missing_edges) > 30:
                lines.append(f"| ... | +{len(res.missing_edges) - 30} more | | |")
            lines.append("")

        if res.unresolved_callees:
            lines.append(f"**Unresolved Callees** ({len(res.unresolved_callees)})")
            lines.append("")
            for u in res.unresolved_callees[:20]:
                lines.append(f"- `{u}`")
            lines.append("")

    return "\n".join(lines)


def _build_mermaid_panel(results: list[dict]) -> str:
    """
    Build Mermaid call tree diagrams rendered inside a self-contained iframe.
    Gradio sandboxes gr.HTML and blocks external scripts, so we use an iframe
    with srcdoc containing a complete HTML page + Mermaid CDN.
    """
    from trustbot.models.agentic import CallGraphOutput
    from trustbot.ui.call_tree import build_mermaid

    flow_sections = []
    for idx, r in enumerate(results):
        neo4j_graph: CallGraphOutput | None = r.get("neo4j_graph")
        index_graph: CallGraphOutput | None = r.get("index_graph")
        flow_name = r["flow_name"]
        trust = r["result"].flow_trust_score

        neo_mermaid = build_mermaid(neo4j_graph) if neo4j_graph and neo4j_graph.edges else ""
        idx_mermaid = build_mermaid(index_graph) if index_graph and index_graph.edges else ""

        if not neo_mermaid and not idx_mermaid:
            continue

        panels_html = ""
        if neo_mermaid:
            panels_html += f"""
            <div style="flex:1;min-width:320px;background:#fff;padding:16px;
                        border-radius:8px;border:2px solid #FF9800;">
                <h4 style="margin:0 0 12px;color:#FF9800;">
                    Agent 1 &mdash; Neo4j ({len(neo4j_graph.edges)} edges)</h4>
                <pre class="mermaid">{neo_mermaid}</pre>
            </div>"""
        if idx_mermaid:
            panels_html += f"""
            <div style="flex:1;min-width:320px;background:#fff;padding:16px;
                        border-radius:8px;border:2px solid #9C27B0;">
                <h4 style="margin:0 0 12px;color:#9C27B0;">
                    Agent 2 &mdash; Index ({len(index_graph.edges)} edges)</h4>
                <pre class="mermaid">{idx_mermaid}</pre>
            </div>"""

        trust_color = "#4CAF50" if trust > 0.7 else "#FF9800" if trust > 0.3 else "#f44336"
        flow_sections.append(f"""
        <div style="margin-bottom:32px;">
            <h3 style="margin:0 0 12px;font-size:17px;border-bottom:1px solid #eee;padding-bottom:8px;">
                Flow {idx+1}: {flow_name}
                <span style="color:{trust_color};font-weight:700;margin-left:12px;">
                    {trust:.0%} trust</span>
            </h3>
            <div style="display:flex;gap:16px;flex-wrap:wrap;">
                {panels_html}
            </div>
        </div>""")

    if not flow_sections:
        return "<p>No call tree diagrams to display.</p>"

    # Build a self-contained HTML page and embed it in an iframe via srcdoc.
    # This bypasses Gradio's script sandboxing.
    inner_html = f"""<!DOCTYPE html>
<html><head>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 16px; background: #fafafa; }}
  .mermaid {{ background: #fff; }}
</style>
</head><body>
<h2 style="margin:0 0 20px;">Call Tree Diagrams</h2>
{''.join(flow_sections)}
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'default',
    flowchart: {{ curve: 'basis', padding: 12 }},
    securityLevel: 'loose'
  }});
</script>
</body></html>"""

    # Escape quotes for srcdoc attribute
    escaped = inner_html.replace("&", "&amp;").replace('"', "&quot;")

    # Calculate a reasonable height based on content
    height = max(400, len(flow_sections) * 350)

    return (
        f'<iframe srcdoc="{escaped}" '
        f'style="width:100%;height:{height}px;border:1px solid #e0e0e0;border-radius:8px;" '
        f'sandbox="allow-scripts"></iframe>'
    )


def _format_agent_output(
    title: str, results: list[dict], graph_key: str
) -> str:
    """Format Agent 1 or Agent 2 output as plain Markdown with full edge tables."""
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
            lines.append("| # | Caller | Class | File | Callee | Class | File | Conf |")
            lines.append("|---|--------|-------|------|--------|-------|------|------|")
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
                lines.append(f"| ... | +{len(graph.edges) - 50} more | | | | | | |")
            lines.append("")
        else:
            lines.append("**No edges found.** Agent 2 could not traverse from the root function.")
            lines.append("This means the root function name from Neo4j did not match any indexed function.")
            lines.append("")

        if graph.unresolved_callees:
            lines.append(f"**Unresolved callees** ({len(graph.unresolved_callees)}):")
            lines.append("")
            for u in graph.unresolved_callees[:30]:
                lines.append(f"- `{u}`")
            lines.append("")

    if not results:
        lines.append("*No flows to display.*")

    return "\n".join(lines)


def _result_to_dict(r: dict) -> dict:
    """Serialize a result dict for JSON output, including full agent data."""
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
                {"caller": e.caller, "callee": e.callee, "trust": e.trust_score,
                 "caller_file": e.caller_file, "callee_file": e.callee_file,
                 "match_type": e.details}
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
