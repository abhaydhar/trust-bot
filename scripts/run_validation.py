"""
Integration test: runs the full TrustBot validation pipeline
for all ExecutionFlows in a project run.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from trustbot.config import settings
from trustbot.tools.base import ToolRegistry
from trustbot.tools.neo4j_tool import Neo4jTool
from trustbot.tools.filesystem_tool import FilesystemTool
from trustbot.agent.orchestrator import AgentOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trustbot.test")

PROJECT_ID = 3151
RUN_ID = 4912


async def main():
    print("=" * 70)
    print("TrustBot Validation -- Project-Level Integration Test")
    print("=" * 70)
    print(f"\nProject ID: {PROJECT_ID}")
    print(f"Run ID:     {RUN_ID}")
    print(f"Neo4j:      {settings.neo4j_uri}")
    print(f"LLM:        {settings.litellm_model} via {settings.litellm_api_base}")
    print()

    # Initialize tools
    registry = ToolRegistry()
    neo4j_tool = Neo4jTool()
    fs_tool = FilesystemTool()

    registry.register(neo4j_tool)
    registry.register(fs_tool)

    print("Initializing Neo4j...")
    await neo4j_tool.initialize()
    print("Initializing Filesystem...")
    await fs_tool.initialize()

    # Index tool — optional
    try:
        from trustbot.tools.index_tool import IndexTool
        index_tool = IndexTool()
        registry.register(index_tool)
        await index_tool.initialize()
        print("Initialized Index (ChromaDB)")
    except Exception as e:
        logger.warning("Index tool not available (non-critical): %s", e)

    # Run project-level validation
    orchestrator = AgentOrchestrator(registry)

    print("\n" + "=" * 70)
    print(f"Validating all ExecutionFlows for project={PROJECT_ID}, run={RUN_ID}")
    print("=" * 70 + "\n")

    report, summary = await orchestrator.process_project(PROJECT_ID, RUN_ID)

    # Print per-flow results
    print("\n" + "=" * 70)
    print("Per-Flow Results")
    print("=" * 70)

    for fr in report.flow_reports:
        fr.compute_summary()
        fs = fr.summary
        status = "PASS" if fs.missing_nodes == 0 and fs.contradicted_edges == 0 else "WARN"
        print(
            f"  [{status}] {fr.execution_flow_name}: "
            f"{fs.valid_nodes}/{fs.total_nodes} nodes valid, "
            f"{fs.confirmed_edges}/{fs.total_edges} edges confirmed"
        )

    # Print overall summary
    s = report.overall_summary
    print("\n" + "=" * 70)
    print("Overall Summary")
    print("=" * 70)
    print(f"  Flows validated: {len(report.flow_reports)}")
    print(f"  Nodes: {s.total_nodes} total -- {s.valid_nodes} valid, {s.drifted_nodes} drifted, {s.missing_nodes} missing")
    print(f"  Edges: {s.total_edges} total -- {s.confirmed_edges} confirmed, {s.unconfirmed_edges} unconfirmed, {s.contradicted_edges} contradicted")

    # Print LLM summary
    print("\n" + "=" * 70)
    print("LLM Summary")
    print("=" * 70)
    print(summary)

    # Save report
    report_path = Path("data/project_validation_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2))
    print(f"\nFull report saved to: {report_path}")

    # Cleanup
    await neo4j_tool.shutdown()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
