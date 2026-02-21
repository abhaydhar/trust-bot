"""Show the actual content of InitialiseEcran chunks to understand why
ChargeArborescence isn't found inside them."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import glob, tempfile
from trustbot.indexing.chunker import chunk_codebase

temp_dirs = glob.glob(str(Path(tempfile.gettempdir()) / "trustbot_git_*"))
repo_path = Path(sorted(temp_dirs)[-1])

chunks = chunk_codebase(repo_path)

# Show InitialiseEcran chunks with FULL content
for c in chunks:
    if c.function_name == "InitialiseEcran" and "011-MultiLevelList" in c.file_path:
        print(f"=== CHUNK: {c.chunk_id} ===")
        print(f"file: {c.file_path}")
        print(f"lines: {c.line_start}-{c.line_end}")
        print(f"language: {c.language}")
        print(f"--- CONTENT ({len(c.content)} chars) ---")
        print(c.content)
        print("--- END ---\n")

# Also show what comes AFTER InitialiseEcran in fMain.pas
fmain_chunks = [c for c in chunks
                if "011-MultiLevelList" in c.file_path and "fMain.pas" in c.file_path]
fmain_chunks.sort(key=lambda c: c.line_start)
print("\n\n=== ALL CHUNKS IN 011-MultiLevelList/src/fMain.pas ===")
for c in fmain_chunks:
    has_charge = "ChargeArborescence" in c.content
    print(f"  lines {c.line_start:4d}-{c.line_end:4d} | {c.function_name:40s} | contains 'ChargeArborescence': {has_charge}")
