"""
Embedding generation via LiteLLM — provider-agnostic.
Supports batching for efficient processing of large chunk sets.
"""

from __future__ import annotations

import logging

import litellm

from trustbot.config import settings
from trustbot.indexing.chunker import CodeChunk

logger = logging.getLogger("trustbot.indexing.embedder")

BATCH_SIZE = 100  # max chunks per embedding API call


def _build_embedding_text(chunk: CodeChunk) -> str:
    """
    Build the text representation sent to the embedding model.
    Includes metadata as a prefix so semantic search can leverage it.
    """
    parts = [
        f"language: {chunk.language}",
        f"file: {chunk.file_path}",
    ]
    if chunk.class_name:
        parts.append(f"class: {chunk.class_name}")
    parts.append(f"function: {chunk.function_name}")
    parts.append("")
    parts.append(chunk.content)
    return "\n".join(parts)


async def embed_chunks(chunks: list[CodeChunk]) -> list[list[float]]:
    """
    Generate embeddings for a list of code chunks using LiteLLM.
    Processes in batches to respect API limits.
    """
    texts = [_build_embedding_text(c) for c in chunks]
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        logger.debug("Embedding batch %d-%d of %d", i, i + len(batch), len(texts))

        response = await litellm.aembedding(
            model=settings.litellm_embedding_model,
            input=batch,
            **settings.get_litellm_kwargs(),
        )

        for item in response.data:
            all_embeddings.append(item["embedding"])

    logger.info("Generated %d embeddings", len(all_embeddings))
    return all_embeddings
