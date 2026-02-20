"""Check what paths are being indexed."""

import sys
sys.path.insert(0, r"c:\Abhay\trust-bot")

import sqlite3
from pathlib import Path

db_path = Path(r"c:\Abhay\trust-bot\sample_codebase\.trustbot_git_index.db")
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Check all unique file paths
cursor.execute("SELECT DISTINCT file_path FROM code_index ORDER BY file_path")
files = [row[0] for row in cursor.fetchall()]

print(f"Total unique files in database: {len(files)}\n")
print("Files indexed:")
for f in files:
    cursor.execute("SELECT COUNT(*) FROM code_index WHERE file_path = ?", (f,))
    count = cursor.fetchone()[0]
    print(f"  {f}: {count} functions")

conn.close()
