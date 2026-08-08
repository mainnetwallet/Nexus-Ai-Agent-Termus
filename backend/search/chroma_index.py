"""ChromaDB-backed VectorIndex. Thin wrapper -- chromadb.Collection
already exposes upsert/query/delete with exactly this shape, so this
class exists only to satisfy the VectorIndex interface (and its
`available = True` flag) rather than to change behavior."""
from __future__ import annotations

from typing import Any

from backend.search.base import VectorIndex


class ChromaVectorIndex(VectorIndex):
    available = True

    def __init__(self, collection: Any) -> None:
        self._collection = collection

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"ids": ids, "documents": documents}
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        self._collection.upsert(**kwargs)

    def query(self, query_texts: list[str], n_results: int = 5) -> dict[str, list[list[Any]]]:
        return self._collection.query(query_texts=query_texts, n_results=n_results)

    def delete(self, ids: list[str]) -> None:
        self._collection.delete(ids=ids)
