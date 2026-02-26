"""
Git repository code indexer.

Clones a git repository, chunks all code files, builds call graphs from chunks.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from trustbot.config import settings
from trustbot.indexing.chunker import chunk_codebase
from trustbot.indexing.call_graph_builder import build_call_graph_from_chunks
from trustbot.index.code_index import CodeIndex

logger = logging.getLogger("trustbot.indexing.git")


class GitCodeIndexer:
    """Index code from a git repository."""

    def __init__(self):
        self._temp_dir = None

    async def clone_and_index(
        self,
        git_url: str,
        branch: str = "main",
        progress_callback: Callable[[float, str], None] | None = None
    ) -> dict:
        """
        Clone a git repo and build code index with call graph.
        
        Returns:
            dict with keys: files, chunks, functions, edges, duration
        """
        start_time = datetime.utcnow()
        
        try:
            # Import gitpython
            import git
        except ImportError:
            raise ImportError("GitPython is required. Install with: pip install gitpython")
        
        # Create temp directory
        self._temp_dir = Path(tempfile.mkdtemp(prefix="trustbot_git_"))
        logger.info(f"Cloning {git_url} to {self._temp_dir}")
        
        if progress_callback:
            progress_callback(0.0, "Cloning repository...")
        
        try:
            repo = git.Repo.clone_from(git_url, self._temp_dir, branch=branch, depth=1)
            
            if progress_callback:
                progress_callback(0.2, "Repository cloned, running Agent 0 (language detection)...")

            from trustbot.agents.agent0_language import Agent0LanguageProfiler
            from trustbot.indexing.chunker import set_language_profiles

            agent0 = Agent0LanguageProfiler(self._temp_dir)
            profiles = await agent0.run()
            if profiles:
                set_language_profiles(profiles)

            if progress_callback:
                progress_callback(0.35, "Scanning files...")
            
            chunks = await asyncio.to_thread(chunk_codebase, self._temp_dir)
            
            if progress_callback:
                progress_callback(0.5, f"Found {len(chunks)} code chunks...")
            
            # Build code index
            code_index = CodeIndex(db_path=settings.codebase_root / ".trustbot_git_index.db")
            code_index.build(codebase_root=self._temp_dir)
            
            # Count functions
            function_count = len([c for c in chunks if c.function_name])
            
            if progress_callback:
                progress_callback(0.7, f"Building call graph from {function_count} functions...")
            
            # Build call graph (LLM-based extraction with cache)
            cache_conn = code_index.get_cache_conn()
            edges = await build_call_graph_from_chunks(chunks, cache_conn=cache_conn)

            # Store edges in the database
            edge_tuples = [(e.from_chunk, e.to_chunk, e.confidence) for e in edges]
            code_index.store_edges(edge_tuples)
            code_index.close()
            
            if progress_callback:
                progress_callback(0.9, f"Found {len(edges)} call relationships...")
            
            # Count unique files
            files = len(set(c.file_path for c in chunks))
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            result = {
                "files": files,
                "chunks": len(chunks),
                "functions": function_count,
                "edges": len(edges),
                "duration": duration,
                "repo_path": str(self._temp_dir),
            }
            
            logger.info(f"Git indexing complete: {result}")
            return result
            
        finally:
            # Keep temp dir for now - clean up later
            pass

    def cleanup(self):
        """Clean up temporary directory."""
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up temp dir: {self._temp_dir}")
