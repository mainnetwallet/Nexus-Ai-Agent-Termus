"""No-op VectorIndex used whenever ChromaDB isn't available (e.g. on
Android/Termux). Every method is a safe no-op so MemoryStore/SkillService
call sites don't need to special-case "chroma missing" everywhere --
only the places that need real semantic recall check `.available` and
fall back to keyword ranking (backend.search.text_rank) themselves."""
from __future__ import annotations

from typing import Any

from backend.search.base import VectorIndex


class NullVectorIndex(VectorIndex):
    available = False

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        return None

    def query(self, query_texts: list[str], n_results: int = 5) -> dict[str, list[list[Any]]]:
        empty = [[] for _ in query_texts]
        return {"ids": empty, "documents": empty, "metadatas": empty, "distances": empty}

    def delete(self, ids: list[str]) -> None:
        return None
