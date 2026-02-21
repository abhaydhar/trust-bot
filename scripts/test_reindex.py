"""
Quick test: re-build the call graph from the already-cloned repo
and check if InitialiseEcran -> ChargeArborescence is detected.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trustbot.indexing.chunker import chunk_codebase
from trustbot.indexing.call_graph_builder import build_call_graph_from_chunks
from trustbot.index.code_index import CodeIndex
from trustbot.config import settings


def main():
    db_path = settings.codebase_root / ".trustbot_git_index.db"

    # Find the cloned repo — look in temp dirs
    import glob
    temp_dirs = glob.glob(str(Path(tempfile.gettempdir()) / "trustbot_git_*"))
    if not temp_dirs:
        print("No cloned repo found in temp. Using sample_codebase as fallback.")
        repo_path = settings.codebase_root.resolve()
    else:
        repo_path = Path(sorted(temp_dirs)[-1])
        print(f"Found cloned repo: {repo_path}")

    print(f"Chunking {repo_path}...")
    chunks = chunk_codebase(repo_path)
    print(f"  {len(chunks)} chunks from {len(set(c.file_path for c in chunks))} files")

    # Show Delphi chunks
    delphi_chunks = [c for c in chunks if c.language == "delphi"]
    print(f"  {len(delphi_chunks)} Delphi chunks")

    print("\nBuilding call graph...")
    edges = build_call_graph_from_chunks(chunks)
    print(f"  {len(edges)} edges")

    print("\n" + "=" * 60)
    print("CHECKING: InitialiseEcran and ChargeArborescence")
    print("=" * 60)

    # Check if InitialiseEcran exists as a chunk
    init_chunks = [c for c in chunks if c.function_name == "InitialiseEcran"]
    print(f"\nInitialiseEcran chunks: {len(init_chunks)}")
    for c in init_chunks:
        print(f"  file={c.file_path}, lines={c.line_start}-{c.line_end}")
        # Show first few lines of content
        lines = c.content.split("\n")[:5]
        for l in lines:
            print(f"    > {l}")

    ca_chunks = [c for c in chunks if c.function_name == "ChargeArborescence"]
    print(f"\nChargeArborescence chunks: {len(ca_chunks)}")
    for c in ca_chunks:
        print(f"  file={c.file_path}, chunk_id={c.chunk_id}")

    # Check edges from InitialiseEcran
    init_edges = [e for e in edges if "InitialiseEcran" in e.from_chunk]
    print(f"\nEdges FROM InitialiseEcran: {len(init_edges)}")
    for e in init_edges:
        print(f"  {e.from_chunk} -> {e.to_chunk} (conf={e.confidence})")

    # Check edges involving ChargeArborescence
    ca_edges = [e for e in edges if "ChargeArborescence" in e.from_chunk or "ChargeArborescence" in e.to_chunk]
    print(f"\nEdges involving ChargeArborescence: {len(ca_edges)}")
    for e in ca_edges:
        print(f"  {e.from_chunk} -> {e.to_chunk} (conf={e.confidence})")

    # Rebuild index
    print("\n\nRebuilding code index...")
    code_index = CodeIndex(db_path=db_path)
    code_index.build(codebase_root=repo_path)

    edge_tuples = [(e.from_chunk, e.to_chunk, e.confidence) for e in edges]
    count = code_index.store_edges(edge_tuples)
    print(f"Stored {count} edges in index DB")
    code_index.close()

    print("\nDone!")


if __name__ == "__main__":
    main()
