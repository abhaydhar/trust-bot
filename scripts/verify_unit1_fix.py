"""Verify that the class-member fallback finds edges for the Unit1 flow."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.agents.agent2_index import Agent2IndexBuilder


async def main():
    db_path = settings.codebase_root / ".trustbot_git_index.db"
    if not db_path.exists():
        print(f"Index DB not found: {db_path}")
        return

    code_index = CodeIndex(db_path=db_path)

    agent2 = Agent2IndexBuilder(code_index)
    result = await agent2.build(
        root_function="Form1",
        execution_flow_id="59bb4d36-test",
        root_class="TForm1",
        root_file="/mnt/storage/.../Unit1.dfm",
    )

    print(f"Root function: {result.root_function}")
    print(f"Edges found: {len(result.edges)}")
    print(f"Unresolved: {len(result.unresolved_callees)}")
    print()
    print("Metadata:")
    for k, v in result.metadata.items():
        if k not in ("sample_index_functions", "sample_edge_callers"):
            print(f"  {k}: {v}")

    print()
    if result.edges:
        print("EDGES:")
        for i, e in enumerate(result.edges, 1):
            print(f"  {i}. {e.caller} -> {e.callee} (file: {e.caller_file} -> {e.callee_file}, conf={e.confidence})")
    else:
        print("NO EDGES FOUND (fallback did not work)")

    if result.unresolved_callees:
        print(f"\nUnresolved: {result.unresolved_callees}")

    code_index.close()


if __name__ == "__main__":
    asyncio.run(main())
