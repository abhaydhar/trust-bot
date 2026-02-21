"""
Debug script: Fetch all data for a single ExecutionFlow from Neo4j.
Usage: .venv\Scripts\python.exe scripts\debug_flow.py
"""
import asyncio
import json
from neo4j import AsyncGraphDatabase

NEO4J_URI = "bolt://rapidx-neo4j-dev.southindia.cloudapp.azure.com:7687/neo4j"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "Rapidxneo4jdev"
FLOW_KEY = "dffe6065-1005-438b-bde9-19b2cd41e1da"


async def main():
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    await driver.verify_connectivity()
    print(f"Connected to Neo4j\n")

    async with driver.session() as session:

        # 1. Fetch the ExecutionFlow node
        print("=" * 80)
        print("STEP 1: ExecutionFlow node")
        print("=" * 80)
        result = await session.run(
            "MATCH (ef:ExecutionFlow {key: $key}) RETURN ef",
            key=FLOW_KEY,
        )
        record = await result.single()
        if not record:
            print(f"NOT FOUND: ExecutionFlow with key={FLOW_KEY}")
            return
        ef = dict(record["ef"])
        print(json.dumps(ef, indent=2, default=str))

        # 2. Fetch ALL Snippet nodes linked to this flow
        print("\n" + "=" * 80)
        print("STEP 2: ALL Snippet nodes linked via PARTICIPATES_IN_FLOW")
        print("=" * 80)
        result = await session.run("""
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
            RETURN s, r, 
                   r.STARTS_FLOW as starts_flow,
                   s.type as snippet_type,
                   s.key as snippet_key,
                   s.function_name as func_name,
                   s.name as snippet_name,
                   s.class_name as class_name,
                   s.file_path as file_path,
                   s.file_name as file_name
            ORDER BY r.STARTS_FLOW DESC, s.type
        """, key=FLOW_KEY)
        snippets = []
        async for rec in result:
            snippet_data = {
                "key": rec["snippet_key"],
                "type": rec["snippet_type"],
                "function_name": rec["func_name"],
                "name": rec["snippet_name"],
                "class_name": rec["class_name"],
                "file_path": rec["file_path"],
                "file_name": rec["file_name"],
                "starts_flow": rec["starts_flow"],
                "all_props": dict(rec["s"]),
            }
            snippets.append(snippet_data)
            is_root = " *** ROOT ***" if rec["snippet_type"] == "ROOT" else ""
            starts = " [STARTS_FLOW]" if rec["starts_flow"] else ""
            print(f"\n  Snippet: {rec['snippet_key']}")
            print(f"    type:          {rec['snippet_type']}{is_root}")
            print(f"    function_name: {rec['func_name']}")
            print(f"    name:          {rec['snippet_name']}")
            print(f"    class_name:    {rec['class_name']}")
            print(f"    file_path:     {rec['file_path']}")
            print(f"    file_name:     {rec['file_name']}")
            print(f"    STARTS_FLOW:   {rec['starts_flow']}{starts}")

        print(f"\n  Total snippets: {len(snippets)}")

        # 3. Fetch CALLS edges between snippets in this flow
        print("\n" + "=" * 80)
        print("STEP 3: CALLS edges between Snippets in this flow")
        print("=" * 80)
        result = await session.run("""
            MATCH (ef:ExecutionFlow {key: $key})<-[:PARTICIPATES_IN_FLOW]-(caller:Snippet)
            MATCH (caller)-[c:CALLS]->(callee:Snippet)
            RETURN caller.key as caller_key,
                   caller.function_name as caller_func,
                   caller.name as caller_name,
                   caller.class_name as caller_class,
                   caller.file_path as caller_file,
                   caller.type as caller_type,
                   callee.key as callee_key,
                   callee.function_name as callee_func,
                   callee.name as callee_name,
                   callee.class_name as callee_class,
                   callee.file_path as callee_file,
                   callee.type as callee_type,
                   properties(c) as call_props
            ORDER BY c.execution_order
        """, key=FLOW_KEY)
        edges = []
        async for rec in result:
            edge = {
                "caller_key": rec["caller_key"],
                "caller_func": rec["caller_func"],
                "caller_name": rec["caller_name"],
                "caller_class": rec["caller_class"],
                "caller_file": rec["caller_file"],
                "caller_type": rec["caller_type"],
                "callee_key": rec["callee_key"],
                "callee_func": rec["callee_func"],
                "callee_name": rec["callee_name"],
                "callee_class": rec["callee_class"],
                "callee_file": rec["callee_file"],
                "callee_type": rec["callee_type"],
                "call_props": rec["call_props"],
            }
            edges.append(edge)
            print(f"\n  EDGE: {rec['caller_func'] or rec['caller_name']} -> {rec['callee_func'] or rec['callee_name']}")
            print(f"    Caller: key={rec['caller_key']}, type={rec['caller_type']}, class={rec['caller_class']}")
            print(f"      func_name={rec['caller_func']}, name={rec['caller_name']}")
            print(f"      file={rec['caller_file']}")
            print(f"    Callee: key={rec['callee_key']}, type={rec['callee_type']}, class={rec['callee_class']}")
            print(f"      func_name={rec['callee_func']}, name={rec['callee_name']}")
            print(f"      file={rec['callee_file']}")
            if rec["call_props"]:
                print(f"    Props: {rec['call_props']}")

        print(f"\n  Total edges: {len(edges)}")

        # 4. Identify ROOT snippet
        print("\n" + "=" * 80)
        print("STEP 4: ROOT identification")
        print("=" * 80)
        result = await session.run("""
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
            WHERE s.type = 'ROOT' AND r.STARTS_FLOW = true
            RETURN s.key as key, s.function_name as func, s.name as name,
                   s.class_name as cls, s.file_path as file, s.type as type
            LIMIT 1
        """, key=FLOW_KEY)
        root_rec = await result.single()
        if root_rec:
            print(f"  ROOT found:")
            print(f"    key:           {root_rec['key']}")
            print(f"    function_name: {root_rec['func']}")
            print(f"    name:          {root_rec['name']}")
            print(f"    class_name:    {root_rec['cls']}")
            print(f"    file_path:     {root_rec['file']}")
        else:
            print("  NO ROOT snippet found (type='ROOT' AND STARTS_FLOW=true)")
            # Try finding any STARTS_FLOW snippet
            result2 = await session.run("""
                MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
                WHERE r.STARTS_FLOW = true
                RETURN s.key as key, s.function_name as func, s.name as name,
                       s.class_name as cls, s.file_path as file, s.type as type
            """, key=FLOW_KEY)
            async for r2 in result2:
                print(f"  STARTS_FLOW snippet (not ROOT type):")
                print(f"    key={r2['key']}, type={r2['type']}")
                print(f"    func={r2['func']}, name={r2['name']}")

    await driver.close()
    print("\n\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
