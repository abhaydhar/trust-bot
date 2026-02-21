"""Debug: trace why root 'Form1' is not found in the index for flow 59bb4d36."""
import asyncio
import sqlite3
import sys
from pathlib import Path
from neo4j import AsyncGraphDatabase

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

NEO4J_URI = "bolt://rapidx-neo4j-dev.southindia.cloudapp.azure.com:7687/neo4j"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "Rapidxneo4jdev"
FLOW_KEY = "59bb4d36-b166-4b82-826a-c31e191b1c66"
DB_PATH = Path("sample_codebase/.trustbot_git_index.db")


async def neo4j_data():
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    await driver.verify_connectivity()

    async with driver.session() as session:
        # ExecutionFlow
        result = await session.run(
            "MATCH (ef:ExecutionFlow {key: $key}) RETURN ef.name as name", key=FLOW_KEY)
        rec = await result.single()
        print(f"ExecutionFlow: {rec['name']}\n")

        # All snippets
        result = await session.run("""
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
            RETURN s.key as key, s.type as type, s.function_name as func,
                   s.name as name, s.class_name as cls, s.file_path as file,
                   s.file_name as fname, r.STARTS_FLOW as starts
            ORDER BY r.STARTS_FLOW DESC
        """, key=FLOW_KEY)
        print("SNIPPETS:")
        snippets = []
        async for r in result:
            snippets.append(dict(r))
            root_tag = " *** ROOT ***" if r["type"] == "ROOT" else ""
            start_tag = " [STARTS_FLOW]" if r["starts"] else ""
            print(f"  key={r['key']}")
            print(f"    type={r['type']}{root_tag}{start_tag}")
            print(f"    function_name={r['func']}")
            print(f"    name={r['name']}")
            print(f"    class_name={r['cls']}")
            print(f"    file_name={r['fname']}")
            print(f"    file_path={r['file']}")
            print()

        # CALLS edges
        result = await session.run("""
            MATCH (ef:ExecutionFlow {key: $key})<-[:PARTICIPATES_IN_FLOW]-(caller:Snippet)
            MATCH (caller)-[c:CALLS]->(callee:Snippet)
            RETURN caller.function_name as caller_func, caller.name as caller_name,
                   callee.function_name as callee_func, callee.name as callee_name
        """, key=FLOW_KEY)
        print("CALLS EDGES:")
        async for r in result:
            cf = r['caller_func'] or r['caller_name']
            ce = r['callee_func'] or r['callee_name']
            print(f"  {cf} -> {ce}")

        # How Agent 1 picks root
        result = await session.run("""
            MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
            WHERE s.type = 'ROOT' AND r.STARTS_FLOW = true
            RETURN s.function_name as func, s.name as name, s.class_name as cls
            LIMIT 1
        """, key=FLOW_KEY)
        root = await result.single()
        if root:
            root_func = root['func'] or root['name']
            print(f"\nAgent 1 ROOT: function_name='{root['func']}', name='{root['name']}', class='{root['cls']}'")
            print(f"  -> Agent 1 will use: '{root['func'] or root['name']}' as root_function")
        else:
            print("\nNo ROOT snippet found. Checking entry_points...")
            result2 = await session.run("""
                MATCH (ef:ExecutionFlow {key: $key})<-[r:PARTICIPATES_IN_FLOW]-(s:Snippet)
                WHERE r.STARTS_FLOW = true
                RETURN s.function_name as func, s.name as name
            """, key=FLOW_KEY)
            async for r2 in result2:
                print(f"  STARTS_FLOW: func={r2['func']}, name={r2['name']}")

    await driver.close()
    return snippets


def check_index(root_name):
    if not DB_PATH.exists():
        print(f"\nIndex DB not found at {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print(f"\n{'='*60}")
    print(f"CHECKING INDEX FOR ROOT: '{root_name}'")
    print(f"{'='*60}")

    # Exact match
    rows = conn.execute(
        "SELECT function_name, file_path, class_name FROM code_index WHERE function_name = ?",
        (root_name,)).fetchall()
    print(f"\n  Exact match for '{root_name}': {len(rows)}")
    for r in rows:
        print(f"    {r['function_name']} | {r['class_name']} | {r['file_path']}")

    # Case-insensitive
    rows = conn.execute(
        "SELECT function_name, file_path, class_name FROM code_index WHERE LOWER(function_name) = LOWER(?)",
        (root_name,)).fetchall()
    print(f"\n  Case-insensitive for '{root_name}': {len(rows)}")
    for r in rows:
        print(f"    {r['function_name']} | {r['class_name']} | {r['file_path']}")

    # Substring/LIKE match
    rows = conn.execute(
        "SELECT function_name, file_path, class_name FROM code_index WHERE function_name LIKE ?",
        (f"%{root_name}%",)).fetchall()
    print(f"\n  LIKE '%{root_name}%': {len(rows)}")
    for r in rows:
        print(f"    {r['function_name']} | {r['class_name']} | {r['file_path']}")

    # Check what IS in Unit1.pas
    rows = conn.execute(
        "SELECT function_name, file_path, class_name FROM code_index WHERE file_path LIKE '%Unit1%'"
    ).fetchall()
    print(f"\n  Functions in any file containing 'Unit1': {len(rows)}")
    for r in rows:
        print(f"    {r['function_name']} | {r['class_name']} | {r['file_path']}")

    # Show all unique function names containing 'Form'
    rows = conn.execute(
        "SELECT DISTINCT function_name, file_path FROM code_index WHERE function_name LIKE '%Form%'"
    ).fetchall()
    print(f"\n  Functions containing 'Form': {len(rows)}")
    for r in rows:
        print(f"    {r['function_name']} | {r['file_path']}")

    conn.close()


async def main():
    snippets = await neo4j_data()

    # Find the root function name Agent 1 would use
    root_snippet = None
    for s in snippets:
        if s.get("type") == "ROOT" and s.get("starts"):
            root_snippet = s
            break
    if not root_snippet:
        for s in snippets:
            if s.get("starts"):
                root_snippet = s
                break

    if root_snippet:
        root_name = root_snippet["func"] or root_snippet["name"] or ""
        # Agent1 code: root_function = root_snippet.function_name or root_snippet.name or root_snippet.id
        print(f"\n>>> Agent 1 would send root_function = '{root_name}' to Agent 2")
        check_index(root_name)
    else:
        print("\nNo root snippet found at all!")


if __name__ == "__main__":
    asyncio.run(main())
