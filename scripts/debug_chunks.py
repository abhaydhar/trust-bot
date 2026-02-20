"""Debug script to see what chunks are being created vs indexed."""

import sys
sys.path.insert(0, r"c:\Abhay\trust-bot")

from pathlib import Path
from trustbot.indexing.chunker import chunk_codebase

# Use a previously cloned repo
repo_path = Path(r"C:\Users\asbhat\AppData\Local\Temp\trustbot_git_5vb5nlck")

if not repo_path.exists():
    print("Repository path doesn't exist!")
    print("Please use the path from the latest git indexing log")
    exit(1)

print(f"Analyzing chunks from: {repo_path}\n")

chunks = chunk_codebase(repo_path)

print(f"Total chunks: {len(chunks)}")

# Analyze chunks
has_function_name = [c for c in chunks if c.function_name]
has_class_name = [c for c in chunks if c.class_name]
has_neither = [c for c in chunks if not c.function_name and not c.class_name]

print(f"Chunks with function_name: {len(has_function_name)}")
print(f"Chunks with class_name only: {len(has_class_name)}")
print(f"Chunks with neither: {len(has_neither)}")

print("\n" + "=" * 60)
print("Sample chunks WITHOUT function names (first 10):")
print("=" * 60)
for chunk in has_neither[:10]:
    print(f"\nFile: {chunk.file_path}")
    print(f"Language: {chunk.language}")
    print(f"Content preview: {chunk.content[:150]}...")

print("\n" + "=" * 60)
print("Sample chunks WITH function names (first 10):")
print("=" * 60)
for chunk in has_function_name[:10]:
    print(f"\nFunction: {chunk.function_name}")
    print(f"File: {chunk.file_path}")
    print(f"Class: {chunk.class_name or 'N/A'}")
