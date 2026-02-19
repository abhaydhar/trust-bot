"""Explore ExecutionFlows by project_id and run_id."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from neo4j import AsyncGraphDatabase
from trustbot.config import settings

PROJECT_ID = 3151
RUN_ID = 4912


async def main():
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    await driver.verify_connectivity()
    print(f"Connected. Querying project_id={PROJECT_ID}, run_id={RUN_ID}\n")

    async with driver.session() as session:
        # Count ExecutionFlow nodes
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})
            RETURN count(ef) as total
            """,
            pid=PROJECT_ID, rid=RUN_ID,
        )
        record = await result.single()
        print(f"Total ExecutionFlow nodes: {record['total']}\n")

        # List them with key info
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})
            RETURN ef.key as key, ef.name as name, ef.module_name as module,
                   ef.complexity as complexity, ef.flow_type as flow_type
            ORDER BY ef.name
            LIMIT 30
            """,
            pid=PROJECT_ID, rid=RUN_ID,
        )
        records = [record async for record in result]
        print(f"First {len(records)} ExecutionFlows:")
        for r in records:
            print(f"  [{r['flow_type']}] {r['name']} (module: {r['module']}, complexity: {r['complexity']})")
            print(f"    key: {r['key']}")

        # Count Snippets linked to these flows
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})
                  <-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
            RETURN count(DISTINCT s) as snippet_count,
                   count(DISTINCT ef) as flow_count
            """,
            pid=PROJECT_ID, rid=RUN_ID,
        )
        record = await result.single()
        print(f"\nTotal unique Snippets across all flows: {record['snippet_count']}")
        print(f"Flows with at least 1 Snippet: {record['flow_count']}")

        # Count CALLS edges
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {project_id: $pid, run_id: $rid})
                  <-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
                  -[c:CALLS]->(target:Snippet)
            RETURN count(c) as call_count
            """,
            pid=PROJECT_ID, rid=RUN_ID,
        )
        record = await result.single()
        print(f"Total CALLS edges: {record['call_count']}")

    await driver.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
