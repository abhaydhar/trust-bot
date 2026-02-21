"""Verify the TForm1 fallback resolves the Unit1 flow."""
import sys, sqlite3
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

db = sqlite3.connect(str(Path('sample_codebase/.trustbot_git_index.db')))
db.row_factory = sqlite3.Row

print('=== Agent 2 root resolution for Unit1 flow ===\n')

print('Step 1: Look up Form1 (original root from Neo4j)')
rows = db.execute(
    "SELECT function_name, file_path, class_name FROM code_index WHERE UPPER(function_name) = 'FORM1'"
).fetchall()
print(f'  Found: {len(rows)} matches\n')

print('Step 2: Form1 not found -> fall back to root_class = TForm1')
rows = db.execute(
    "SELECT function_name, file_path, class_name FROM code_index WHERE UPPER(function_name) = 'TFORM1'"
).fetchall()
print(f'  Found: {len(rows)} matches')
for r in rows:
    print(f'    {r[0]} | class={r[2]} | {r[1]}')

print('\nStep 3: Check if TForm1 has outgoing edges')
edges = db.execute("SELECT caller, callee FROM call_edges WHERE caller LIKE '%TForm1%'").fetchall()
print(f'  TForm1 edges: {len(edges)}')
for e in edges[:10]:
    print(f'    {e[0]} -> {e[1]}')
if len(edges) > 10:
    print(f'    ... +{len(edges)-10} more')

print('\nStep 4: Will traversal reach Button2Click -> TraitementDeLaBase?')
b2c = db.execute("SELECT caller, callee FROM call_edges WHERE caller LIKE '%Button2Click%'").fetchall()
print(f'  Button2Click edges: {len(b2c)}')
for e in b2c:
    print(f'    {e[0]} -> {e[1]}')

db.close()
