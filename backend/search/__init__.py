"""
Vector/semantic search abstraction.

MemoryStore (backend/memory/store.py) and SkillService
(backend/skills/library.py) both need a small "upsert / query / delete"
index over free text (workflow summaries, skill descriptions) for
semantic recall. On Windows/Linux/macOS that's backed by ChromaDB; on
Android/Termux, where ChromaDB is often unavailable, `get_vector_index()`
transparently hands back a no-op index instead so callers never have to
branch on platform to stay safe.

A no-op index alone would silently lose semantic recall, though --
that's what `backend.search.text_rank` is for: a keyword-relevance
ranking helper that MemoryStore/SkillService call directly, against
their own existing SQLite tables, whenever `index.available` is False.
There is deliberately no generic "SQLite-backed VectorIndex" here --
requests 5/6/7 of the Termux compatibility work call for ranking over
the *existing* MemoryEntry/Skill tables (no duplicate storage), and the
result shape differs enough between memories and skills (different
scoring fields, different output records) that forcing them through one
generic index class would just be an extra layer of indirection around
`text_rank.rank_candidates()`, not a reduction in duplicated logic.
"""
from backend.search.base import VectorIndex
from backend.search.factory import get_vector_index

__all__ = ["VectorIndex", "get_vector_index"]
