"""
get_vector_index() -- the one place that decides whether a given named
collection ("nexus_workflows", "nexus_skills") is backed by ChromaDB or
by the no-op index, based on `backend.platform_info.capabilities`.

A single lazily-created chromadb.PersistentClient is shared across every
collection requested here (same persist directory, same process) rather
than each caller constructing its own client -- purely an internal
cleanup; behavior/persistence location is unchanged from before (both
MemoryStore and SkillService already pointed at `settings.chroma_persist_dir`
and the "nexus_workflows"/"nexus_skills" collection names).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.platform_info import capabilities
from backend.search.base import VectorIndex
from backend.search.null_index import NullVectorIndex

logger = logging.getLogger("nexus.search")

_client: Any = None


def _get_chroma_client() -> Any:
    global _client
    if _client is None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        from backend.config.settings import settings

        _client = chromadb.PersistentClient(
            path=settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _client


def get_vector_index(collection_name: str, embedding_function: Optional[Any] = None) -> VectorIndex:
    """Returns a ChromaVectorIndex for `collection_name` when ChromaDB is
    available on this platform, otherwise a NullVectorIndex. Never raises
    -- if ChromaDB is reported available by platform_info but fails to
    initialize anyway (corrupt persist dir, etc.), this logs and degrades
    to the no-op index rather than taking the whole Agent down."""
    if not capabilities.chromadb_available:
        return NullVectorIndex()

    try:
        from backend.search.chroma_index import ChromaVectorIndex

        client = _get_chroma_client()
        collection_kwargs: dict[str, Any] = {}
        if embedding_function is not None:
            collection_kwargs["embedding_function"] = embedding_function
        collection = client.get_or_create_collection(collection_name, **collection_kwargs)
        return ChromaVectorIndex(collection)
    except Exception:  # noqa: BLE001 - any chroma init failure degrades, never crashes
        logger.exception("ChromaDB unavailable for collection %s; falling back to no-op index", collection_name)
        return NullVectorIndex()
