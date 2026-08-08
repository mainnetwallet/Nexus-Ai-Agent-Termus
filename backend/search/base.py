"""
VectorIndex: the interface MemoryStore and SkillService code against
instead of talking to a chromadb collection directly.

Deliberately shaped like chromadb.Collection's own upsert/query/delete
signatures (including the query() return shape -- a dict of
same-length-nested-lists keyed by "ids"/"documents"/"metadatas"/
"distances") so existing call sites built against the raw chroma
collection keep working almost unchanged; the only new thing callers
need is the `available` flag, to decide whether to additionally run a
keyword-ranking fallback (see backend.search.text_rank) for cases where
an empty/no-op result isn't good enough on its own (e.g. workflow
recall, skill matching).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class VectorIndex(ABC):
    #: False for a no-op index (e.g. ChromaDB unavailable on this
    #: platform) -- callers that need real semantic recall rather than an
    #: empty result should check this and fall back to keyword ranking
    #: over their own SQLite data instead.
    available: bool = True

    @abstractmethod
    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        """Insert or replace embeddings for the given ids. Must never
        raise on a healthy no-op index -- callers rely on being able to
        call this unconditionally without guarding every call site."""

    @abstractmethod
    def query(self, query_texts: list[str], n_results: int = 5) -> dict[str, list[list[Any]]]:
        """Same return shape as chromadb.Collection.query(): a dict with
        "ids"/"documents"/"metadatas"/"distances" keys, each a list (one
        per query text) of lists (one per result). A no-op index returns
        the same shape with empty inner lists."""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """Remove embeddings for the given ids. Must never raise on a
        healthy no-op index."""
