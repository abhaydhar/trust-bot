"""Gradio-based web UI for TrustBot."""

from __future__ import annotations

import json
import logging

import gradio as gr

from trustbot.agent.orchestrator import AgentOrchestrator
from trustbot.models.validation import EdgeVerdict, NodeVerdict, ProjectValidationReport
from trustbot.tools.base import ToolRegistry

logger = logging.getLogger("trustbot.ui")


def create_ui(registry: ToolRegistry) -> gr.Blocks:
    orchestrator = AgentOrchestrator(registry)

    async def validate_project(project_id_str: str, run_id_str: str):
        if not project_id_str.strip() or not run_id_str.strip():
            return "Please enter both Project ID and Run ID.", "", ""
        try:
            project_id = int(project_id_str.strip())
            run_id = int(run_id_str.strip())
        except ValueError:
            return "Project ID and Run ID must be integers.", "", ""
        try:
            report, summary = await orchestrator.process_project(project_id, run_id)
            report_md = _format_project_report_markdown(report)
            raw_json = report.model_dump_json(indent=2)
            return summary, report_md, raw_json
        except ValueError as e:
            return f"Error: {e}", "", ""
        except Exception as e:
            logger.exception("Validation failed")
            return f"Unexpected error: {e}", "", ""

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
            status = await index_tool.call("get_index_status")
            return json.dumps(status, indent=2)
        except Exception as e:
            return f"Error: {e}"

    app = gr.Blocks(title="TrustBot")

    with app:
        gr.Markdown("# TrustBot\n*Validate Neo4j call graphs against your actual codebase*")

        with gr.Tabs():
            with gr.Tab("Validate"):
                with gr.Row():
                    project_id_input = gr.Textbox(label="Project ID", placeholder="e.g. 3151", scale=2)
                    run_id_input = gr.Textbox(label="Run ID", placeholder="e.g. 4912", scale=2)
                    validate_btn = gr.Button("Validate All Flows", variant="primary", scale=1)

                summary_output = gr.Markdown(label="Summary")

                with gr.Accordion("Detailed Report", open=False):
                    report_output = gr.Markdown(label="Structured Report")

                with gr.Accordion("Raw JSON", open=False):
                    json_output = gr.Code(label="Report JSON", language="json")

                validate_btn.click(
                    fn=validate_project,
                    inputs=[project_id_input, run_id_input],
                    outputs=[summary_output, report_output, json_output],
                )

            with gr.Tab("Chat"):
                gr.Markdown("Ask TrustBot questions about execution flows, code, or the knowledge graph.")
                chat_input = gr.Textbox(label="Your Question", placeholder="Ask TrustBot...", lines=2)
                chat_btn = gr.Button("Send", variant="primary")
                chat_output = gr.Markdown(label="Response")

                chat_btn.click(fn=handle_chat, inputs=chat_input, outputs=chat_output)

            with gr.Tab("Index"):
                gr.Markdown("### Codebase Index Management")
                with gr.Row():
                    index_btn = gr.Button("Incremental Re-index")
                    force_index_btn = gr.Button("Full Re-index", variant="secondary")
                    status_btn = gr.Button("Check Status")
                index_output = gr.Textbox(label="Result", lines=6, interactive=False)

                index_btn.click(fn=lambda: run_reindex(False), outputs=index_output)
                force_index_btn.click(fn=lambda: run_reindex(True), outputs=index_output)
                status_btn.click(fn=get_status, outputs=index_output)

    return app


def _format_project_report_markdown(report: ProjectValidationReport) -> str:
    lines = [
        f"# Project Validation Report",
        f"**Project ID**: {report.project_id} | **Run ID**: {report.run_id} | "
        f"**Flows validated**: {len(report.flow_reports)}\n",
    ]

    s = report.overall_summary
    lines.append("## Overall Summary\n")
    lines.append(f"- **Nodes**: {s.total_nodes} total -- {s.valid_nodes} valid, {s.drifted_nodes} drifted, {s.missing_nodes} missing")
    lines.append(f"- **Edges**: {s.total_edges} total -- {s.confirmed_edges} confirmed, {s.unconfirmed_edges} unconfirmed, {s.contradicted_edges} contradicted")

    for fr in report.flow_reports:
        fr.compute_summary()
        fs = fr.summary
        lines.append(f"\n---\n## Flow: {fr.execution_flow_name}")
        lines.append(f"*Key: `{fr.execution_flow_key}`*\n")

        if fr.node_results:
            lines.append("### Nodes\n")
            lines.append("| Function | File | Verdict | Confidence | Details |")
            lines.append("|----------|------|---------|------------|---------|")
            for n in fr.node_results:
                verdict_icon = {NodeVerdict.VALID: "OK", NodeVerdict.DRIFTED: "DRIFT", NodeVerdict.MISSING: "MISS"}.get(n.verdict, "?")
                lines.append(f"| `{n.function_name}` | `{n.file_path}` | {verdict_icon} | {n.confidence:.0%} | {n.details} |")

        if fr.edge_results:
            lines.append("\n### Edges\n")
            lines.append("| Caller | Callee | Verdict | Confidence | Details |")
            lines.append("|--------|--------|---------|------------|---------|")
            for e in fr.edge_results:
                verdict_icon = {EdgeVerdict.CONFIRMED: "OK", EdgeVerdict.UNCONFIRMED: "?", EdgeVerdict.CONTRADICTED: "FAIL"}.get(e.verdict, "?")
                lines.append(f"| `{e.caller_function}` | `{e.callee_function}` | {verdict_icon} | {e.confidence:.0%} | {e.details} |")

        lines.append(f"\n**Flow summary**: {fs.valid_nodes}/{fs.total_nodes} nodes valid, {fs.confirmed_edges}/{fs.total_edges} edges confirmed")

    return "\n".join(lines)
