from __future__ import annotations

import logging

import chromadb
import litellm

from trustbot.config import settings
from trustbot.indexing.pipeline import COLLECTION_NAME, IndexingPipeline
from trustbot.tools.base import BaseTool

logger = logging.getLogger("trustbot.tools.index")


class IndexTool(BaseTool):
    """
    Tool for semantic search over the indexed codebase.
    Wraps ChromaDB and the indexing pipeline for the agent.
    """

    name = "index"
    description = (
        "Search the indexed codebase using semantic search or exact metadata filters. "
        "Can find functions by name, search code by meaning, and report index health."
    )

    def __init__(self) -> None:
        super().__init__()
        self._pipeline: IndexingPipeline | None = None
        self._collection: chromadb.Collection | None = None

    async def initialize(self) -> None:
        self._pipeline = IndexingPipeline()
        self._collection = self._pipeline.collection
        logger.info(
            "Index tool initialized. Current index has %d chunks.",
            self._collection.count(),
        )

    async def shutdown(self) -> None:
        self._pipeline = None
        self._collection = None

    @property
    def collection(self) -> chromadb.Collection:
        if self._collection is None:
            raise RuntimeError("Index tool not initialized.")
        return self._collection

    @property
    def pipeline(self) -> IndexingPipeline:
        if self._pipeline is None:
            raise RuntimeError("Index tool not initialized.")
        return self._pipeline

    async def search_code(self, query: str, top_k: int = 10) -> list[dict]:
        """
        Semantic search over the indexed codebase.
        Returns ranked results with file path, function name, line numbers, and content.
        """
        # Generate query embedding
        response = await litellm.aembedding(
            model=settings.litellm_embedding_model,
            input=[query],
            **settings.get_litellm_kwargs(),
        )
        query_embedding = response.data[0]["embedding"]

        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, 20),
            include=["documents", "metadatas", "distances"],
        )

        formatted: list[dict] = []
        if results["ids"] and results["ids"][0]:
            for i, chunk_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                formatted.append({
                    "chunk_id": chunk_id,
                    "file_path": meta.get("file_path", ""),
                    "function_name": meta.get("function_name", ""),
                    "class_name": meta.get("class_name", ""),
                    "language": meta.get("language", ""),
                    "line_start": meta.get("line_start", 0),
                    "line_end": meta.get("line_end", 0),
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "distance": results["distances"][0][i] if results["distances"] else 0,
                })

        return formatted

    async def search_function(
        self,
        function_name: str,
        class_name: str | None = None,
        file_path: str | None = None,
    ) -> list[dict]:
        """
        Find a specific function/method by name using metadata filters.
        Supports optional class name and file path for more precise lookups.
        """
        where_filter: dict = {"function_name": {"$eq": function_name}}
        if class_name:
            where_filter = {
                "$and": [
                    {"function_name": {"$eq": function_name}},
                    {"class_name": {"$eq": class_name}},
                ]
            }
        if file_path:
            path_filter = {"file_path": {"$eq": file_path}}
            if "$and" in where_filter:
                where_filter["$and"].append(path_filter)
            else:
                where_filter = {
                    "$and": [
                        {"function_name": {"$eq": function_name}},
                        path_filter,
                    ]
                }

        results = self.collection.get(
            where=where_filter,
            include=["documents", "metadatas"],
            limit=10,
        )

        formatted: list[dict] = []
        if results["ids"]:
            for i, chunk_id in enumerate(results["ids"]):
                meta = results["metadatas"][i] if results["metadatas"] else {}
                formatted.append({
                    "chunk_id": chunk_id,
                    "file_path": meta.get("file_path", ""),
                    "function_name": meta.get("function_name", ""),
                    "class_name": meta.get("class_name", ""),
                    "language": meta.get("language", ""),
                    "line_start": meta.get("line_start", 0),
                    "line_end": meta.get("line_end", 0),
                    "content": results["documents"][i] if results["documents"] else "",
                })

        return formatted

    async def reindex(self, force: bool = False) -> dict:
        """Trigger a re-index of the codebase. Use force=True to rebuild from scratch."""
        return await self.pipeline.run(force=force)

    async def get_index_status(self) -> dict:
        """Return current index health and statistics."""
        return self.pipeline.get_status()
