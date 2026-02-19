"""
Indexing pipeline: chunks the codebase, generates embeddings, and stores
them in ChromaDB for semantic search.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import chromadb

from trustbot.config import settings
from trustbot.indexing.chunker import CodeChunk, chunk_codebase
from trustbot.indexing.embedder import embed_chunks

logger = logging.getLogger("trustbot.indexing.pipeline")

COLLECTION_NAME = "trustbot_code"


class IndexingPipeline:
    """
    Orchestrates the full indexing flow:
    1. Walk the codebase and chunk files into function-level pieces
    2. Generate embeddings via LiteLLM
    3. Upsert into ChromaDB with metadata for filtered search
    """

    def __init__(self) -> None:
        persist_dir = str(settings.chroma_persist_dir.resolve())
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def collection(self) -> chromadb.Collection:
        return self._collection

    async def run(self, force: bool = False) -> dict:
        """
        Run the full indexing pipeline.

        Args:
            force: If True, re-index everything. Otherwise, only index changed files.

        Returns:
            Stats dict with counts of files/chunks processed.
        """
        root = settings.codebase_root.resolve()
        logger.info("Starting indexing of %s", root)

        chunks = chunk_codebase(root)
        if not chunks:
            logger.warning("No code chunks found in %s", root)
            return {"files": 0, "chunks": 0, "new": 0, "skipped": 0}

        if not force:
            chunks = self._filter_unchanged(chunks)

        if not chunks:
            logger.info("All chunks are up to date, nothing to index.")
            return {"files": 0, "chunks": 0, "new": 0, "skipped": 0}

        # Generate embeddings
        embeddings = await embed_chunks(chunks)

        # Prepare data for ChromaDB
        ids = [self._chunk_id(c) for c in chunks]
        documents = [c.content for c in chunks]
        metadatas = [
            {
                "file_path": c.file_path,
                "function_name": c.function_name,
                "class_name": c.class_name,
                "language": c.language,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "content_hash": self._content_hash(c.content),
            }
            for c in chunks
        ]

        # Upsert in batches (ChromaDB limit is ~5000 per call)
        batch_size = 5000
        for i in range(0, len(ids), batch_size):
            self._collection.upsert(
                ids=ids[i : i + batch_size],
                embeddings=embeddings[i : i + batch_size],
                documents=documents[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
            )

        unique_files = len({c.file_path for c in chunks})
        logger.info("Indexed %d chunks from %d files", len(chunks), unique_files)

        return {
            "files": unique_files,
            "chunks": len(chunks),
            "new": len(chunks),
            "skipped": 0,
        }

    def _filter_unchanged(self, chunks: list[CodeChunk]) -> list[CodeChunk]:
        """Skip chunks whose content hash already matches what's in the store."""
        changed: list[CodeChunk] = []
        for chunk in chunks:
            chunk_id = self._chunk_id(chunk)
            content_hash = self._content_hash(chunk.content)

            try:
                existing = self._collection.get(ids=[chunk_id], include=["metadatas"])
                if existing["metadatas"] and existing["metadatas"][0]:
                    if existing["metadatas"][0].get("content_hash") == content_hash:
                        continue
            except Exception:
                pass

            changed.append(chunk)

        logger.info(
            "Incremental index: %d changed, %d unchanged",
            len(changed),
            len(chunks) - len(changed),
        )
        return changed

    def _chunk_id(self, chunk: CodeChunk) -> str:
        raw = f"{chunk.file_path}::{chunk.class_name}::{chunk.function_name}::{chunk.line_start}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _content_hash(self, content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()

    def get_status(self) -> dict:
        """Return current index statistics."""
        count = self._collection.count()
        return {
            "collection": COLLECTION_NAME,
            "total_chunks": count,
            "persist_dir": str(settings.chroma_persist_dir),
        }
