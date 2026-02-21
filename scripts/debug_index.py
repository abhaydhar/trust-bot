"""
Debug script: Check what the code index has for the functions in this flow.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("sample_codebase/.trustbot_git_index.db")

if not DB_PATH.exists():
    print(f"Index DB not found at {DB_PATH}")
    print("You need to clone+index the repo first via the Code Indexer tab.")
    sys.exit(1)

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

print("=" * 80)
print("ALL INDEXED FUNCTIONS (code_index table)")
print("=" * 80)
rows = conn.execute("SELECT function_name, file_path, class_name, language FROM code_index ORDER BY function_name").fetchall()
print(f"Total: {len(rows)} functions\n")
for r in rows:
    print(f"  {r['function_name']:40s} | {r['class_name'] or '-':20s} | {r['file_path']}")

print("\n" + "=" * 80)
print("SEARCHING FOR: InitialiseEcran, ChargeArborescence")
print("=" * 80)
for name in ["InitialiseEcran", "ChargeArborescence"]:
    exact = conn.execute(
        "SELECT * FROM code_index WHERE function_name = ?", (name,)
    ).fetchall()
    icase = conn.execute(
        "SELECT * FROM code_index WHERE LOWER(function_name) = LOWER(?)", (name,)
    ).fetchall()
    print(f"\n  '{name}':")
    print(f"    Exact match: {len(exact)} rows")
    for r in exact:
        print(f"      -> file={r['file_path']}, class={r['class_name']}")
    print(f"    Case-insensitive: {len(icase)} rows")
    for r in icase:
        print(f"      -> file={r['file_path']}, class={r['class_name']}")

print("\n" + "=" * 80)
print("ALL CALL EDGES (call_edges table)")
print("=" * 80)
edges = conn.execute("SELECT caller, callee, confidence FROM call_edges ORDER BY caller").fetchall()
print(f"Total: {len(edges)} edges\n")
for e in edges:
    print(f"  {e['caller']}")
    print(f"    -> {e['callee']}  (conf={e['confidence']})")
    print()

# Search for edges involving our functions
print("=" * 80)
print("EDGES involving InitialiseEcran or ChargeArborescence")
print("=" * 80)
for name in ["InitialiseEcran", "ChargeArborescence"]:
    pattern = f"%{name}%"
    matching = conn.execute(
        "SELECT * FROM call_edges WHERE caller LIKE ? OR callee LIKE ?",
        (pattern, pattern),
    ).fetchall()
    print(f"\n  Edges containing '{name}': {len(matching)}")
    for e in matching:
        print(f"    {e['caller']} -> {e['callee']} (conf={e['confidence']})")

conn.close()
print("\nDone.")
