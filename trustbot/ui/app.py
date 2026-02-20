"""Enhanced Gradio-based web UI for TrustBot with charts, progress tracking, and code indexer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import gradio as gr

from trustbot.agent.orchestrator import AgentOrchestrator
from trustbot.agents.pipeline import ValidationPipeline
from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.models.validation import EdgeVerdict, NodeVerdict, ProjectValidationReport
from trustbot.tools.base import ToolRegistry

logger = logging.getLogger("trustbot.ui")


def create_ui(registry: ToolRegistry, code_index: CodeIndex | None = None) -> gr.Blocks:
    orchestrator = AgentOrchestrator(registry)
    pipeline = None
    git_index = None  # Track git-cloned repository index
    
    if code_index:
        try:
            pipeline = ValidationPipeline(
                neo4j_tool=registry.get("neo4j"),
                filesystem_tool=registry.get("filesystem"),
                code_index=code_index,
            )
        except KeyError:
            logger.warning("Multi-agent pipeline not available (missing tools)")

    # Capture the main event loop for thread-safe async operations
    main_loop = asyncio.get_event_loop()

    def _run_async(coro):
        """Run coroutine in the main event loop from any thread."""
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        return future.result()

    async def validate_project(project_id_str: str, run_id_str: str, progress=gr.Progress()):
        if not project_id_str.strip() or not run_id_str.strip():
            return "Please enter both Project ID and Run ID.", "", "", None, None
        try:
            project_id = int(project_id_str.strip())
            run_id = int(run_id_str.strip())
        except ValueError:
            return "Project ID and Run ID must be integers.", "", "", None, None
        
        try:
            progress(0, desc="Starting validation...")
            
            # Get flows first to track progress
            neo4j_tool = registry.get("neo4j")
            flows = await neo4j_tool.call("get_execution_flows_by_project", project_id=project_id, run_id=run_id)
            total_flows = len(flows)
            
            progress(0.1, desc=f"Found {total_flows} flows to validate...")
            
            # Validate with progress updates
            report, summary = await orchestrator.process_project(project_id, run_id, progress_callback=lambda i, t: progress((0.1 + 0.8 * i / t), desc=f"Validating flow {i+1}/{t}..."))
            
            progress(0.9, desc="Generating report...")
            
            report_md = _format_project_report_markdown(report)
            raw_json = report.model_dump_json(indent=2)
            
            # Generate charts
            progress(0.95, desc="Creating visualizations...")
            node_chart = _create_node_chart(report)
            edge_chart = _create_edge_chart(report)
            
            progress(1.0, desc="Complete!")
            
            return summary, report_md, raw_json, node_chart, edge_chart
        except ValueError as e:
            return f"Error: {e}", "", "", None, None
        except Exception as e:
            logger.exception("Validation failed")
            return f"Unexpected error: {e}", "", "", None, None

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

    async def validate_agentic(flow_key: str, progress=gr.Progress()):
        """Run multi-agent dual-derivation validation."""
        if not flow_key.strip():
            return "Please enter an Execution Flow key.", ""
        if not pipeline:
            return "Multi-agent pipeline not available.", ""
        
        try:
            progress(0, desc="Starting dual-derivation...")
            progress(0.2, desc="Agent 1: Fetching from Neo4j...")
            progress(0.4, desc="Agent 2: Building from filesystem...")
            progress(0.6, desc="Normalizing graphs...")
            progress(0.8, desc="Verification in progress...")
            
            result, report_md = await pipeline.validate(flow_key.strip())
            
            progress(1.0, desc="Complete!")
            
            summary = f"**Trust Score**: {result.flow_trust_score:.0%}\n\n"
            summary += f"Confirmed: {len(result.confirmed_edges)} | "
            summary += f"Phantom: {len(result.phantom_edges)} | "
            summary += f"Missing: {len(result.missing_edges)}"
            return summary, report_md
        except Exception as e:
            logger.exception("Agentic validation failed")
            return f"Error: {e}", ""

    async def clone_and_index_repo(git_url: str, branch: str, progress=gr.Progress()):
        """Clone a git repo and build code index."""
        nonlocal git_index
        
        if not git_url.strip():
            return "Please enter a Git repository URL."
        
        try:
            progress(0, desc="Cloning repository...")
            
            # Import here to avoid loading gitpython unless needed
            from trustbot.indexing.git_indexer import GitCodeIndexer
            
            indexer = GitCodeIndexer()
            
            progress(0.2, desc="Downloading code...")
            result = await indexer.clone_and_index(git_url.strip(), branch.strip() or "main", 
                                                   progress_callback=lambda p, d: progress(0.2 + 0.6 * p, desc=d))
            
            progress(0.9, desc="Finalizing...")
            
            # Update git_index to point to the newly created index
            git_index_path = settings.codebase_root / ".trustbot_git_index.db"
            git_index = CodeIndex(db_path=git_index_path)
            
            output = f"""
## Indexing Complete!

**Repository**: {git_url}
**Branch**: {branch or 'main'}
**Files processed**: {result['files']}
**Code chunks created**: {result['chunks']}
**Functions indexed**: {result['functions']}
**Call graph edges**: {result['edges']}
**Duration**: {result['duration']:.1f}s

The code has been chunked and indexed. Use the "Chunk Visualizer" tab to explore.
"""
            
            progress(1.0, desc="Done!")
            return output
            
        except ImportError:
            return "Error: GitPython not installed. Run: pip install gitpython"
        except Exception as e:
            logger.exception("Git indexing failed")
            return f"Error: {e}"

    async def get_chunk_data():
        """Get chunk visualization data."""
        try:
            from trustbot.indexing.chunk_visualizer import ChunkVisualizer
            
            # Use git_index if available, otherwise use main code_index
            active_index = git_index if git_index else code_index
            viz = ChunkVisualizer(active_index)
            graph_data = await viz.get_graph_data()
            
            return graph_data
        except Exception as e:
            logger.exception("Chunk visualization failed")
            return {"nodes": [], "edges": []}

    # Main Gradio app
    app = gr.Blocks(title="TrustBot")

    with app:
        gr.Markdown("# TrustBot\n*AI-powered call graph validation with multi-agent architecture*")

        with gr.Tabs():
            # Tab 1: Validate (Enhanced)
            with gr.Tab("Validate"):
                with gr.Row():
                    project_id_input = gr.Textbox(label="Project ID", placeholder="e.g. 3151", scale=2)
                    run_id_input = gr.Textbox(label="Run ID", placeholder="e.g. 4912", scale=2)
                    validate_btn = gr.Button("Validate All Flows", variant="primary", scale=1)

                summary_output = gr.Markdown(label="Summary")
                
                # Charts row
                with gr.Row():
                    node_chart = gr.BarPlot(
                        label="Node Validation Results",
                        x="status",
                        y="count",
                        color="status",
                        visible=False
                    )
                    edge_chart = gr.BarPlot(
                        label="Edge Validation Results",
                        x="status",
                        y="count",
                        color="status",
                        visible=False
                    )

                with gr.Accordion("Detailed Report", open=False):
                    report_output = gr.Markdown(label="Structured Report")

                with gr.Accordion("Raw JSON", open=False):
                    json_output = gr.Code(label="Report JSON", language="json")

                validate_btn.click(
                    fn=lambda p, r: _run_async(validate_project(p, r)),
                    inputs=[project_id_input, run_id_input],
                    outputs=[summary_output, report_output, json_output, node_chart, edge_chart],
                )

            # Tab 2: Code Indexer (NEW)
            with gr.Tab("Code Indexer"):
                gr.Markdown("""
                ### Git Repository Indexer
                Clone any git repository, chunk the code, and build a call graph automatically.
                This creates an indexed codebase for use with Agent 2 (filesystem validation).
                """)
                
                with gr.Row():
                    git_url_input = gr.Textbox(
                        label="Git Repository URL",
                        placeholder="https://github.com/username/repo.git",
                        scale=3
                    )
                    branch_input = gr.Textbox(
                        label="Branch",
                        placeholder="main",
                        value="main",
                        scale=1
                    )
                
                index_repo_btn = gr.Button("Clone and Index Repository", variant="primary")
                index_status = gr.Markdown(label="Status")
                
                index_repo_btn.click(
                    fn=lambda u, b: _run_async(clone_and_index_repo(u, b)),
                    inputs=[git_url_input, branch_input],
                    outputs=[index_status]
                )

            # Tab 3: Chunk Visualizer (NEW)
            with gr.Tab("Chunk Visualizer"):
                gr.Markdown("""
                ### Code Chunk Graph
                Browse all indexed code chunks and their call relationships.
                Index a repository first using the **Code Indexer** tab.
                """)
                
                # Hidden state to hold full data
                all_nodes_state = gr.State([])
                all_edges_state = gr.State([])
                node_page_state = gr.State(1)
                edge_page_state = gr.State(1)
                
                with gr.Row():
                    refresh_btn = gr.Button("Refresh Visualization", variant="primary", scale=2)
                    page_size_dd = gr.Dropdown(
                        choices=["25", "50", "100", "200"],
                        value="50",
                        label="Rows per page",
                        interactive=True,
                        scale=1,
                    )
                
                chunk_stats = gr.Markdown(value="Click **Refresh** after indexing a repository.")
                
                with gr.Tabs():
                    with gr.Tab("Functions"):
                        node_page_info = gr.Markdown(value="")
                        chunk_table = gr.Dataframe(
                            headers=["#", "Function", "File", "Language", "Type"],
                            datatype=["str", "str", "str", "str", "str"],
                            label="Indexed Functions",
                            interactive=False,
                            wrap=True,
                        )
                        with gr.Row():
                            node_prev_btn = gr.Button("Previous", size="sm")
                            node_next_btn = gr.Button("Next", size="sm")
                    
                    with gr.Tab("Call Relationships"):
                        edge_page_info = gr.Markdown(value="")
                        edge_table = gr.Dataframe(
                            headers=["#", "Caller", "Callee", "Confidence"],
                            datatype=["str", "str", "str", "str"],
                            label="Call Graph Edges",
                            interactive=False,
                            wrap=True,
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
                
                def _node_rows_from_data(nodes):
                    rows = []
                    for i, n in enumerate(nodes, 1):
                        rows.append([
                            str(i),
                            str(n.get("name", "")),
                            str(n.get("file", "")).replace("\\", "/"),
                            str(n.get("language", "")),
                            str(n.get("type", "function")),
                        ])
                    return rows
                
                def _edge_rows_from_data(edges):
                    rows = []
                    for i, e in enumerate(edges, 1):
                        rows.append([
                            str(i),
                            _short_chunk_id(str(e.get("from", ""))),
                            _short_chunk_id(str(e.get("to", ""))),
                            str(e.get("confidence", "")),
                        ])
                    return rows
                
                def render_chunk_viz(page_size_str="50"):
                    try:
                        page_size = int(page_size_str or "50")
                        data = _run_async(get_chunk_data())
                        nodes = data.get("nodes", [])
                        edges = data.get("edges", [])
                        
                        stats = f"**Total Functions**: {len(nodes)} | **Total Call Relationships**: {len(edges)}"
                        
                        node_rows = _node_rows_from_data(nodes)
                        edge_rows = _edge_rows_from_data(edges)
                        
                        n_slice, n_pg, n_total = _paginate(node_rows, 1, page_size)
                        e_slice, e_pg, e_total = _paginate(edge_rows, 1, page_size)
                        
                        n_info = f"Page **{n_pg}** of **{n_total}** ({len(nodes)} functions)"
                        e_info = f"Page **{e_pg}** of **{e_total}** ({len(edges)} edges)"
                        
                        return (
                            nodes, edges,       # states
                            1, 1,               # page resets
                            stats,
                            n_info, n_slice,
                            e_info, e_slice,
                        )
                    except Exception as e:
                        logger.exception("Chunk visualization failed")
                        return [], [], 1, 1, f"Error: {e}", "", [], "", []
                
                refresh_btn.click(
                    fn=render_chunk_viz,
                    inputs=[page_size_dd],
                    outputs=[
                        all_nodes_state, all_edges_state,
                        node_page_state, edge_page_state,
                        chunk_stats,
                        node_page_info, chunk_table,
                        edge_page_info, edge_table,
                    ],
                )
                
                def go_node_page(nodes, current_page, delta, page_size_str):
                    page_size = int(page_size_str)
                    new_page = current_page + delta
                    node_rows = _node_rows_from_data(nodes)
                    sliced, pg, total_pages = _paginate(node_rows, new_page, page_size)
                    info = f"Page **{pg}** of **{total_pages}** ({len(nodes)} functions)"
                    return pg, info, sliced
                
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
                
                def go_edge_page(edges, current_page, delta, page_size_str):
                    page_size = int(page_size_str)
                    new_page = current_page + delta
                    edge_rows = _edge_rows_from_data(edges)
                    sliced, pg, total_pages = _paginate(edge_rows, new_page, page_size)
                    info = f"Page **{pg}** of **{total_pages}** ({len(edges)} edges)"
                    return pg, info, sliced
                
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

            # Tab 4: Agentic (Dual-Derivation)
            with gr.Tab("Agentic (Dual-Derivation)"):
                gr.Markdown(
                    "**Multi-agent validation**: Agent 1 fetches from Neo4j, Agent 2 builds from "
                    "filesystem independently. Verification Agent diffs and scores."
                )
                with gr.Row():
                    flow_key_input = gr.Textbox(
                        label="Execution Flow Key",
                        placeholder="e.g. EF-001 or flow key from Neo4j",
                        scale=3,
                    )
                    agentic_btn = gr.Button("Run Dual-Derivation", variant="primary", scale=1)
                agentic_summary = gr.Markdown(label="Summary")
                agentic_report = gr.Markdown(label="Report")
                agentic_btn.click(
                    fn=lambda f: _run_async(validate_agentic(f)),
                    inputs=[flow_key_input],
                    outputs=[agentic_summary, agentic_report],
                )

            # Tab 5: Chat
            with gr.Tab("Chat"):
                gr.Markdown("Ask TrustBot questions about execution flows, code, or the knowledge graph.")
                chat_input = gr.Textbox(label="Your Question", placeholder="Ask TrustBot...", lines=2)
                chat_btn = gr.Button("Send", variant="primary")
                chat_output = gr.Markdown(label="Response")

                chat_btn.click(fn=lambda m: _run_async(handle_chat(m)), inputs=chat_input, outputs=chat_output)

            # Tab 6: Index Management
            with gr.Tab("Index Management"):
                gr.Markdown("### Codebase Index Management")
                with gr.Row():
                    index_btn = gr.Button("Incremental Re-index")
                    force_index_btn = gr.Button("Full Re-index", variant="secondary")
                    status_btn = gr.Button("Check Status")
                index_output = gr.Textbox(label="Result", lines=6, interactive=False)

                index_btn.click(fn=lambda: _run_async(run_reindex(False)), outputs=index_output)
                force_index_btn.click(fn=lambda: _run_async(run_reindex(True)), outputs=index_output)
                status_btn.click(fn=lambda: _run_async(get_status()), outputs=index_output)

    return app


def _format_project_report_markdown(report: ProjectValidationReport) -> str:
    """Format project report with collapsible sections."""
    lines = [
        f"# Project Validation Report",
        f"**Project ID**: {report.project_id} | **Run ID**: {report.run_id} | "
        f"**Flows validated**: {len(report.flow_reports)}\n",
    ]

    s = report.overall_summary
    lines.append("## Overall Summary\n")
    lines.append(f"- **Nodes**: {s.total_nodes} total -- {s.valid_nodes} valid, {s.drifted_nodes} drifted, {s.missing_nodes} missing")
    lines.append(f"- **Edges**: {s.total_edges} total -- {s.confirmed_edges} confirmed, {s.unconfirmed_edges} unconfirmed, {s.contradicted_edges} contradicted\n")

    # Add collapsible flow sections
    for idx, fr in enumerate(report.flow_reports):
        fr.compute_summary()
        fs = fr.summary
        
        # Flow header with key
        lines.append(f"\n<details{'open' if idx < 3 else ''}>")
        lines.append(f"<summary><b>Flow {idx+1}/{len(report.flow_reports)}: {fr.execution_flow_name}</b> (Key: <code>{fr.execution_flow_key}</code>) - {fs.valid_nodes}/{fs.total_nodes} nodes, {fs.confirmed_edges}/{fs.total_edges} edges</summary>\n")

        if fr.node_results:
            lines.append("### Nodes\n")
            lines.append("| Function | File | Verdict | Confidence | Details |")
            lines.append("|----------|------|---------|------------|---------|")
            for n in fr.node_results:
                verdict_icon = {NodeVerdict.VALID: "✓", NodeVerdict.DRIFTED: "⚠", NodeVerdict.MISSING: "✗"}.get(n.verdict, "?")
                lines.append(f"| `{n.function_name}` | `{n.file_path}` | {verdict_icon} | {n.confidence:.0%} | {n.details} |")

        if fr.edge_results:
            lines.append("\n### Edges\n")
            lines.append("| Caller | Callee | Verdict | Confidence | Details |")
            lines.append("|--------|--------|---------|------------|---------|")
            for e in fr.edge_results:
                verdict_icon = {EdgeVerdict.CONFIRMED: "✓", EdgeVerdict.UNCONFIRMED: "?", EdgeVerdict.CONTRADICTED: "✗"}.get(e.verdict, "?")
                lines.append(f"| `{e.caller_function}` | `{e.callee_function}` | {verdict_icon} | {e.confidence:.0%} | {e.details} |")

        lines.append(f"\n**Flow summary**: {fs.valid_nodes}/{fs.total_nodes} nodes valid, {fs.confirmed_edges}/{fs.total_edges} edges confirmed\n")
        lines.append("</details>")

    return "\n".join(lines)


def _create_node_chart(report: ProjectValidationReport) -> dict:
    """Create node validation chart data."""
    s = report.overall_summary
    return {
        "status": ["Valid", "Drifted", "Missing"],
        "count": [s.valid_nodes, s.drifted_nodes, s.missing_nodes],
    }


def _create_edge_chart(report: ProjectValidationReport) -> dict:
    """Create edge validation chart data."""
    s = report.overall_summary
    return {
        "status": ["Confirmed", "Unconfirmed", "Contradicted"],
        "count": [s.confirmed_edges, s.unconfirmed_edges, s.contradicted_edges],
    }


def _generate_chunk_html(data: dict) -> str:
    """Generate simple HTML visualization for chunks."""
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    
    if not nodes:
        return "<p>No chunks available. Use the Code Indexer tab to index a repository first.</p>"
    
    html = f'<div><h3>Code Chunks ({len(nodes)} total)</h3>'
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin:20px 0;">'
    
    for node in nodes[:50]:
        name = str(node.get('name', 'Unknown')).replace('<', '&lt;').replace('>', '&gt;')
        file_path = str(node.get('file', '')).replace('\\', '/').replace('<', '&lt;').replace('>', '&gt;')
        if len(file_path) > 30:
            file_path = file_path[:30] + "..."
        html += (
            '<div style="border:2px solid #4CAF50;padding:10px;border-radius:8px;background:#f9f9f9;">'
            f'<div style="font-weight:bold;font-size:14px;margin-bottom:5px;">{name}</div>'
            f'<div style="font-size:11px;color:#666;">{file_path}</div>'
            '</div>'
        )
    
    html += "</div>"
    
    if len(nodes) > 50:
        html += f"<p><i>Showing 50 of {len(nodes)} chunks</i></p>"
    
    if edges:
        html += f'<div style="margin-top:20px;"><h3>Call Relationships ({len(edges)} total)</h3>'
        for edge in edges[:20]:
            from_node = str(edge.get('from', '?')).replace('<', '&lt;').replace('>', '&gt;')
            to_node = str(edge.get('to', '?')).replace('<', '&lt;').replace('>', '&gt;')
            html += (
                '<div style="padding:8px;border-left:3px solid #FF9800;margin:5px 0;background:#fff3e0;">'
                f'<b>{from_node}</b> &rarr; {to_node}'
                '</div>'
            )
        html += "</div>"
        
        if len(edges) > 20:
            html += f"<p><i>Showing 20 of {len(edges)} relationships</i></p>"
    
    html += "</div>"
    
    return html


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
