"""
Exploration script: connects to Neo4j, fetches the node with the given key,
and discovers connected nodes (Snippets, relationships, call graph).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from neo4j import AsyncGraphDatabase
from trustbot.config import settings

KEY = "28363924-96fc-40e8-87bc-2c725be91e18"


async def main():
    print(f"Connecting to Neo4j at {settings.neo4j_uri}...")
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )

    try:
        await driver.verify_connectivity()
        print("Connected successfully!\n")
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    async with driver.session() as session:

        # Step 1: Fetch the ExecutionFlow node
        print(f"=== Step 1: ExecutionFlow node ===")
        result = await session.run(
            "MATCH (ef:ExecutionFlow {key: $key}) RETURN ef",
            key=KEY,
        )
        record = await result.single()
        if not record:
            print("NOT FOUND!")
            return

        ef = dict(record["ef"])
        print(f"Name: {ef.get('name')}")
        print(f"Type: {ef.get('type')}")
        print(f"Module: {ef.get('module_name')}")
        print(f"Program files: {ef.get('program_files')}")
        print(f"Layers map: {ef.get('layers_map')}")
        print(f"Children map: {ef.get('children_map')}")
        print(f"Parent map: {ef.get('parent_map')}")

        # Step 2: Get all directly connected nodes (outgoing)
        print(f"\n=== Step 2: Outgoing relationships ===")
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {key: $key})-[r]->(m)
            RETURN type(r) as rel_type, properties(r) as rel_props,
                   labels(m) as target_labels, properties(m) as target_props
            """,
            key=KEY,
        )
        records = [record async for record in result]
        print(f"Outgoing: {len(records)}")
        for r in records:
            tp = dict(r["target_props"])
            print(f"  -[{r['rel_type']}]-> {r['target_labels']}")
            print(f"    key={tp.get('key', '?')}, name={tp.get('name', tp.get('function_name', '?'))}")
            rel_props = dict(r["rel_props"]) if r["rel_props"] else {}
            if rel_props:
                print(f"    rel props: {json.dumps(rel_props, default=str)}")

        # Step 3: Get all directly connected nodes (incoming)
        print(f"\n=== Step 3: Incoming relationships ===")
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {key: $key})<-[r]-(m)
            RETURN type(r) as rel_type, properties(r) as rel_props,
                   labels(m) as source_labels, properties(m) as source_props
            """,
            key=KEY,
        )
        records = [record async for record in result]
        print(f"Incoming: {len(records)}")
        for r in records:
            sp = dict(r["source_props"])
            print(f"  <-[{r['rel_type']}]- {r['source_labels']}")
            print(f"    key={sp.get('key', '?')}, name={sp.get('name', sp.get('function_name', '?'))}")
            rel_props = dict(r["rel_props"]) if r["rel_props"] else {}
            if rel_props:
                print(f"    rel props: {json.dumps(rel_props, default=str)}")

        # Step 4: Specifically look at PARTICIPATES_IN_FLOW relationships
        print(f"\n=== Step 4: PARTICIPATES_IN_FLOW relationships ===")
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s)
            RETURN labels(s) as labels, properties(r) as rel_props, properties(s) as snippet_props
            ORDER BY r.order
            """,
            key=KEY,
        )
        records = [record async for record in result]
        print(f"Participants: {len(records)}")
        for r in records:
            sp = dict(r["snippet_props"])
            rp = dict(r["rel_props"]) if r["rel_props"] else {}
            print(f"  Labels: {r['labels']}")
            print(f"    Rel props: {json.dumps(rp, default=str)}")
            # Print key snippet properties
            for field in ["key", "function_name", "name", "class_name", "file_path",
                          "language", "line_start", "line_end", "type", "module_name",
                          "source_file", "STARTS_FLOW", "starts_flow", "file_name"]:
                val = sp.get(field)
                if val is not None:
                    print(f"    {field}: {val}")
            print()

        # Step 5: Look at CALLS relationships from participating snippets
        print(f"\n=== Step 5: Call graph from participants ===")
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {key: $key})<-[:PARTICIPATES_IN_FLOW]-(s)
            OPTIONAL MATCH (s)-[c:CALLS]->(target)
            RETURN properties(s) as caller_props, labels(s) as caller_labels,
                   type(c) as call_type, properties(c) as call_props,
                   properties(target) as callee_props, labels(target) as callee_labels
            """,
            key=KEY,
        )
        records = [record async for record in result]
        print(f"Call relationships: {len(records)}")
        for r in records:
            cp = dict(r["caller_props"])
            caller_name = cp.get("function_name", cp.get("name", "?"))
            if r["callee_props"]:
                tp = dict(r["callee_props"])
                callee_name = tp.get("function_name", tp.get("name", "?"))
                call_props = dict(r["call_props"]) if r["call_props"] else {}
                print(f"  {caller_name} -[{r['call_type']}]-> {callee_name}")
                if call_props:
                    print(f"    call props: {json.dumps(call_props, default=str)}")
            else:
                print(f"  {caller_name} -> (no outgoing CALLS)")

        # Step 6: Get a sample Snippet to understand its full property set
        print(f"\n=== Step 6: Sample Snippet node properties ===")
        result = await session.run(
            """
            MATCH (ef:ExecutionFlow {key: $key})<-[:PARTICIPATES_IN_FLOW]-(s:Snippet)
            RETURN properties(s) as props
            LIMIT 1
            """,
            key=KEY,
        )
        record = await result.single()
        if record:
            props = dict(record["props"])
            print(f"Full Snippet properties ({len(props)} fields):")
            for k, v in sorted(props.items()):
                val_str = str(v)
                if len(val_str) > 200:
                    val_str = val_str[:200] + "..."
                print(f"  {k}: {val_str}")

    await driver.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
