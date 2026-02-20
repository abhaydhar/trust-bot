"""Quick diagnostic to check database schema and contents."""

import sqlite3
from pathlib import Path

db_path = Path(r"c:\Abhay\trust-bot\sample_codebase\.trustbot_git_index.db")

if not db_path.exists():
    print("[ERROR] Git index database doesn't exist yet")
    print("   Run git indexer first!")
    exit(1)

conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Check schema
print("=" * 60)
print("DATABASE SCHEMA")
print("=" * 60)
cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='code_index'")
schema = cursor.fetchone()
if schema:
    print(schema[0])
else:
    print("[ERROR] Table 'code_index' not found!")

# Check indexes
print("\n" + "=" * 60)
print("INDEXES")
print("=" * 60)
cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='code_index'")
for name, sql in cursor.fetchall():
    print(f"{name}: {sql}")

# Count rows
print("\n" + "=" * 60)
print("DATA STATISTICS")
print("=" * 60)
cursor.execute("SELECT COUNT(*) FROM code_index")
total = cursor.fetchone()[0]
print(f"Total functions: {total}")

cursor.execute("SELECT COUNT(DISTINCT function_name) FROM code_index")
unique_names = cursor.fetchone()[0]
print(f"Unique function names: {unique_names}")

cursor.execute("SELECT COUNT(DISTINCT file_path) FROM code_index")
unique_files = cursor.fetchone()[0]
print(f"Unique files: {unique_files}")

# Show duplicates
print("\n" + "=" * 60)
print("DUPLICATE FUNCTION NAMES (Top 10)")
print("=" * 60)
cursor.execute("""
    SELECT function_name, COUNT(*) as count 
    FROM code_index 
    GROUP BY function_name 
    HAVING count > 1 
    ORDER BY count DESC 
    LIMIT 10
""")
for name, count in cursor.fetchall():
    print(f"{name}: {count} occurrences")

# Sample data
print("\n" + "=" * 60)
print("SAMPLE DATA (First 10 rows)")
print("=" * 60)
cursor.execute("SELECT id, function_name, file_path FROM code_index LIMIT 10")
for row in cursor.fetchall():
    print(f"ID={row[0]}: {row[1]} @ {row[2]}")

conn.close()

print("\n" + "=" * 60)
if total == 194:
    print("[SUCCESS] All 194 functions indexed correctly!")
elif total == 61:
    print("[FAILURE] Old schema still active (only unique names)")
else:
    print(f"[WARNING] Unexpected count: {total} functions")
print("=" * 60)
