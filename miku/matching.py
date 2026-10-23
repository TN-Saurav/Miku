from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .models import Track

_STOP_WORDS = {"a", "an", "and", "by", "of", "the", "to", "with"}
_VARIANT_PENALTIES = {
    "cover": 0.24,
    "karaoke": 0.32,
    "instrumental": 0.20,
    "remix": 0.17,
    "live": 0.15,
    "slowed": 0.16,
    "sped up": 0.16,
    "nightcore": 0.17,
    "acoustic": 0.08,
    "lyrics": 0.07,
}


def _normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    ascii_folded = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_folded))


def _tokens(value: str) -> set[str]:
    return {token for token in _normalized(value).split() if token not in _STOP_WORDS}


def _token_similarity(query_token: str, candidate_token: str) -> float:
    if query_token == candidate_token:
        return 1.0
    if min(len(query_token), len(candidate_token)) < 4:
        return 0.0
    similarity = SequenceMatcher(None, query_token, candidate_token).ratio()
    return similarity if similarity >= 0.85 else 0.0


def _token_coverage(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    pairs = sorted(
        (
            (_token_similarity(query_token, candidate_token), query_token, candidate_token)
            for query_token in query_tokens
            for candidate_token in candidate_tokens
        ),
        reverse=True,
    )
    used_query: set[str] = set()
    used_candidate: set[str] = set()
    matched = 0.0
    for similarity, query_token, candidate_token in pairs:
        if similarity == 0.0:
            break
        if query_token in used_query or candidate_token in used_candidate:
            continue
        used_query.add(query_token)
        used_candidate.add(candidate_token)
        matched += similarity
    return matched / len(query_tokens)


def match_score(query: str, track: Track) -> float:
    """Deterministic metadata score; an unknown source is never called official."""
    query_text = _normalized(query)
    query_tokens = _tokens(query)
    title_text = _normalized(track.title)
    title_tokens = _tokens(track.title)
    artist_tokens = _tokens(track.artist or "")
    if not query_text or not query_tokens:
        return 0.0

    candidate_tokens = title_tokens | artist_tokens
    query_coverage = _token_coverage(query_tokens, candidate_tokens)
    title_coverage = _token_coverage(query_tokens, title_tokens)
    title_similarity = SequenceMatcher(None, query_text, title_text).ratio()
    score = 0.54 * title_similarity + 0.31 * query_coverage + 0.15 * title_coverage

    requested_variants = _tokens(query)
    candidate_text = _normalized(f"{track.title} {track.artist or ''}")
    for variant, penalty in _VARIANT_PENALTIES.items():
        variant_tokens = set(variant.split())
        if variant_tokens & requested_variants:
            # The requested variant is intentional; do not penalize it.
            if variant_tokens <= requested_variants:
                score += 0.06
            continue
        if variant in candidate_text:
            score -= penalty

    # Leave headroom so verified source metadata can break a true content tie.
    score = min(score, 0.92)
    if track.official_source is True:
        score += 0.08
    return max(0.0, min(score, 1.0))


def rank_tracks(query: str, tracks: tuple[Track, ...] | list[Track]) -> tuple[Track, ...]:
    """Rank by title/artist metadata, preserving provider order for equal scores."""
    return tuple(sorted(tracks, key=lambda track: match_score(query, track), reverse=True))


def confident_match(query: str, tracks: tuple[Track, ...] | list[Track], *,
                    minimum: float = 0.72, margin: float = 0.12) -> Track | None:
    ranked = rank_tracks(query, tracks)
    if not ranked or match_score(query, ranked[0]) < minimum:
        return None
    if len(ranked) > 1 and match_score(query, ranked[0]) - match_score(query, ranked[1]) < margin:
        return None
    return ranked[0]
