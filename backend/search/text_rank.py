"""
Keyword relevance ranking -- the SQLite-backed fallback search used by
MemoryStore.recall_similar_workflows() and SkillService.semantic_search()
when ChromaDB isn't available (see backend.search.get_vector_index).

Shared by both callers so the ranking rules (normalize -> exact phrase >
exact word > partial match) live in exactly one place instead of being
reimplemented per-domain. Callers pass in their own list of candidate
dicts (already fetched from MemoryEntry/Skill via normal SQLAlchemy
queries -- no separate index table) plus a {field_name: weight} map
describing which of their own fields to search and how much each one
should count.
"""
from __future__ import annotations

import re
from typing import Any

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Relative credit for each match tier -- exact phrase beats every word
# present beats a partial/substring hit. Blended per-field, then
# combined across fields by the caller-supplied weights.
_PHRASE_MATCH_SCORE = 1.0
_ALL_WORDS_MATCH_SCORE = 0.65
_PARTIAL_WORD_MATCH_MAX = 0.3
_SUBSTRING_MATCH_MAX = 0.15


def normalize(text: str) -> str:
    """lowercase, strip punctuation, collapse whitespace."""
    text = (text or "").lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _score_field(query_norm: str, query_words: list[str], field_norm: str) -> float:
    if not field_norm or not query_norm:
        return 0.0

    if query_norm in field_norm:
        # Exact phrase present (the whole field being exactly the query
        # scores no higher -- phrase containment is already the top tier).
        return _PHRASE_MATCH_SCORE

    if not query_words:
        return 0.0

    field_words = set(field_norm.split())
    matched_words = sum(1 for w in query_words if w in field_words)
    if matched_words == len(query_words):
        return _ALL_WORDS_MATCH_SCORE
    if matched_words > 0:
        return _PARTIAL_WORD_MATCH_MAX * (matched_words / len(query_words))

    substring_hits = sum(1 for w in query_words if w and w in field_norm)
    if substring_hits > 0:
        return _SUBSTRING_MATCH_MAX * (substring_hits / len(query_words))
    return 0.0


def score_candidate(query_norm: str, query_words: list[str], candidate: dict[str, Any], field_weights: dict[str, float]) -> float:
    total = 0.0
    weight_sum = 0.0
    for field, weight in field_weights.items():
        if weight <= 0:
            continue
        raw = candidate.get(field)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raw = str(raw)
        total += _score_field(query_norm, query_words, normalize(raw)) * weight
        weight_sum += weight
    return (total / weight_sum) if weight_sum else 0.0


def rank_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    field_weights: dict[str, float],
    top_k: int = 5,
) -> list[tuple[dict[str, Any], float]]:
    """Ranks `candidates` (plain dicts) against `query` using a weighted
    blend of `field_weights` (field name -> relative importance). Returns
    up to `top_k` (candidate, score) pairs with score > 0, best first.
    Ties are stable (Python sort), i.e. input order is preserved among
    equal scores -- callers should pass candidates pre-sorted by recency
    or importance so ties favor the more relevant/recent one."""
    query_norm = normalize(query)
    query_words = [w for w in query_norm.split() if w]
    if not query_norm:
        return []

    scored = [
        (cand, score_candidate(query_norm, query_words, cand, field_weights))
        for cand in candidates
    ]
    scored = [(cand, score) for cand, score in scored if score > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_k]
