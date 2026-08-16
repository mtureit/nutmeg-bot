"""OpenAI Embeddings isolation layer (PLAN.md 5, 9).

The signature `embed(texts) -> vectors` is deliberately unchanged from the
earlier local-model design: index_build.py and retriever.py must not know
whether embedding happens locally or over the network.
"""

from __future__ import annotations

import logging

from openai import OpenAI

import config

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        config.require("OPENAI_API_KEY")
        # The SDK retries 429/5xx with exponential backoff internally.
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            max_retries=config.API_MAX_RETRIES,
        )
    return _client


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batching to stay inside the API request limits."""
    if not texts:
        return []

    client = _get_client()
    vectors: list[list[float]] = []

    for start in range(0, len(texts), config.EMBED_BATCH_SIZE):
        batch = texts[start : start + config.EMBED_BATCH_SIZE]
        response = client.embeddings.create(
            model=config.EMBED_MODEL,
            input=batch,
            dimensions=config.EMBED_DIM,
        )
        # The API preserves input order, but sort by index to be explicit.
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors.extend(item.embedding for item in ordered)
        logger.debug("embedded %d/%d", len(vectors), len(texts))

    return vectors


def embed_query(text: str) -> list[float]:
    """Embed a single question. No prefix is needed for OpenAI models."""
    return embed([text])[0]
