from __future__ import annotations

import logging

from .database import Database, normalize_query
from .models import PlayDecision, SearchOutcome, Track
from .matching import match_score, rank_tracks
from .providers import ProviderRegistry

log = logging.getLogger(__name__)


class SongMemoryService:
    def __init__(self, database: Database, providers: ProviderRegistry):
        self.database = database
        self.providers = providers

    async def search(self, query: str) -> SearchOutcome:
        if not normalize_query(query) or len(query) > 200:
            return SearchOutcome((), ("Enter a song query between 1 and 200 characters.",))
        tracks, warnings = await self.providers.search(query, limit=5)
        return SearchOutcome(tuple(tracks[:5]), tuple(warnings))

    async def search_playable(self, query: str, *, limit: int = 4) -> SearchOutcome:
        if not normalize_query(query) or len(query) > 200:
            return SearchOutcome((), ("Enter a song query between 1 and 200 characters.",))
        tracks, warnings = await self.providers.search(query, limit=5, playback_only=True)
        eligible = [track for track in tracks if self.providers.supports_playback(track)]
        ranked = [track for track in rank_tracks(query, eligible) if match_score(query, track) >= 0.42]
        return SearchOutcome(tuple(ranked[:max(1, min(limit, 5))]), tuple(warnings))

    async def auto_play_request(self, guild_id: int, user_id: int, query: str) -> PlayDecision:
        """Play the best currently resolvable result from an enabled audio source."""
        if not normalize_query(query):
            return PlayDecision("search", notice="Enter a song name to search.", search=SearchOutcome(()))
        if len(query) > 200:
            return PlayDecision(
                "search", notice="Song queries must be 200 characters or fewer.", search=SearchOutcome(())
            )

        outcome = await self.search_playable(query, limit=4)
        if not outcome.tracks:
            return PlayDecision("search", search=outcome)
        for selected in outcome.tracks:
            try:
                source = await self.providers.resolve(selected)
            except Exception as exc:
                log.warning(
                    "Could not resolve YouTube search result %s (%s)",
                    selected.stable_id, type(exc).__name__,
                )
                source = None
            if source is not None:
                return PlayDecision("play_match", selected, search=outcome, source=source)
        unavailable = SearchOutcome(
            outcome.tracks,
            (*outcome.warnings,
             "YouTube search results were found, but Lavalink could not load playable audio. "
             "Try another public video or retry later."),
        )
        return PlayDecision("search", search=unavailable)


