import pytest

from miku.database import Database
from miku.models import PlayableSource, Track
from miku.providers import MusicProvider, ProviderError, ProviderRegistry
from miku.service import SongMemoryService


class FakeProvider(MusicProvider):
    name = "local"
    playback_search = True

    def __init__(self, tracks):
        self.tracks = tracks
        self.unavailable = set()
        self.resolve_errors = set()
        self.search_calls = []
        self.fail_search = False

    async def search(self, query, limit=5):
        self.search_calls.append(query)
        if self.fail_search:
            raise ProviderError("mock provider failed")
        return self.tracks[:limit]

    async def resolve(self, track):
        if track.stable_id in self.unavailable:
            return None
        return PlayableSource("file", f"C:/music/{track.stable_id}.mp3")

    def supports_playback(self, track):
        return track.provider == "local" and track.stable_id not in self.unavailable


class FakeYouTubeProvider(FakeProvider):
    name = "youtube"

    async def resolve(self, track):
        if track.stable_id in self.resolve_errors:
            raise RuntimeError("upstream load failure")
        if track.stable_id in self.unavailable:
            return None
        return PlayableSource("lavalink", f"encoded:{track.stable_id}")

    def supports_playback(self, track):
        return track.provider == self.name and track.stable_id not in self.unavailable


@pytest.fixture
def tracks():
    return [
        Track("local", "version-one", "Kalyani (live)", "Artist A", 180),
        Track("local", "version-two", "Kalyani", "Artist B", 210),
    ]


@pytest.mark.asyncio
async def test_provider_failure_is_reported_without_exposing_details(database, tracks):
    provider = FakeProvider(tracks)
    provider.fail_search = True
    result = await SongMemoryService(database, ProviderRegistry([provider])).search("Kalyani")
    assert result.tracks == ()
    assert result.warnings == ("mock provider failed",)


@pytest.mark.asyncio
async def test_empty_or_oversized_search_does_not_list_the_local_library(database, tracks):
    provider = FakeProvider(tracks)
    service = SongMemoryService(database, ProviderRegistry([provider]))
    assert (await service.search("   ")).tracks == ()
    assert (await service.search("x" * 201)).tracks == ()
    assert provider.search_calls == []


@pytest.mark.asyncio
async def test_auto_play_chooses_a_confident_exact_playable_match(database, tracks):
    service = SongMemoryService(database, ProviderRegistry([FakeProvider(tracks)]))

    decision = await service.auto_play_request(1, 2, "Kalyani")

    assert decision.action == "play_match"
    assert decision.track == tracks[1]
    assert decision.source.kind == "file"


@pytest.mark.asyncio
async def test_auto_play_uses_current_best_match(database, tracks):
    provider = FakeProvider(tracks)
    service = SongMemoryService(database, ProviderRegistry([provider]))

    decision = await service.auto_play_request(1, 2, " KALYANI! ")

    assert decision.action == "play_match"
    assert decision.track == tracks[1]
    assert decision.source.kind == "file"
    assert provider.search_calls == [" KALYANI! "]


@pytest.mark.asyncio
async def test_auto_play_uses_best_ranked_match_even_when_matches_are_tied(database):
    tracks = [
        Track("local", "one", "Kalyani", "Artist One"),
        Track("local", "two", "Kalyani", "Artist Two"),
    ]
    service = SongMemoryService(database, ProviderRegistry([FakeProvider(tracks)]))

    decision = await service.auto_play_request(1, 2, "Kalyani")

    assert decision.action == "play_match"
    assert decision.track == tracks[0]
    assert decision.source.kind == "file"


@pytest.mark.asyncio
async def test_auto_play_chooses_current_playable_match(database, tracks):
    service = SongMemoryService(database, ProviderRegistry([FakeProvider(tracks)]))

    decision = await service.auto_play_request(1, 2, "Kalyani")

    assert decision.action == "play_match"
    assert decision.track == tracks[1]
    assert decision.source.kind == "file"


@pytest.mark.asyncio
async def test_auto_play_keeps_singular_query_eligible_for_plural_youtube_title(database):
    track = Track(
        "youtube", "AbCdEfGhI_1", "Alan Walker - Headlights (Official Music Video)", "Alan Walker",
    )
    service = SongMemoryService(database, ProviderRegistry([FakeYouTubeProvider([track])]))

    decision = await service.auto_play_request(1, 2, "headlight")

    assert decision.action == "play_match"
    assert decision.track == track
    assert decision.source.kind == "lavalink"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("headlight", "Alan Walker - Headlights (Official Music Video)"),
        ("begin", "Måneskin - Beggin' (Official Video)"),
        ("beggin", "Madcon - Beggin' (Official Video)"),
    ],
)
async def test_auto_play_accepts_close_title_spelling_and_partial_variants(database, query, title):
    track = Track("youtube", "matched-video", title, "Uploader")
    service = SongMemoryService(database, ProviderRegistry([FakeYouTubeProvider([track])]))

    decision = await service.auto_play_request(1, 2, query)

    assert decision.action == "play_match"
    assert decision.track == track
    assert decision.source.kind == "lavalink"


@pytest.mark.asyncio
async def test_auto_play_rejects_a_weak_spelling_neighbor(database):
    unrelated = Track("youtube", "weak-match", "Began - New Song", "Uploader")
    service = SongMemoryService(database, ProviderRegistry([FakeYouTubeProvider([unrelated])]))

    decision = await service.auto_play_request(1, 2, "begin")

    assert decision.action == "search"
    assert decision.track is None


@pytest.mark.asyncio
async def test_auto_play_skips_unavailable_match_and_uses_best_available_match(database, tracks):
    provider = FakeProvider(tracks)
    provider.unavailable.add("version-one")
    service = SongMemoryService(database, ProviderRegistry([provider]))

    decision = await service.auto_play_request(1, 2, "Kalyani")

    assert decision.action == "play_match"
    assert decision.track == tracks[1]
    assert decision.source.kind == "file"


@pytest.mark.asyncio
async def test_youtube_auto_play_tries_next_ranked_result_when_best_load_fails(database):
    candidates = [
        Track("youtube", "first-video", "Kalyani", "Official Channel"),
        Track("youtube", "second-video", "Kalyani", "Another Channel"),
    ]
    provider = FakeYouTubeProvider(candidates)
    provider.resolve_errors.add("first-video")
    service = SongMemoryService(database, ProviderRegistry([provider]))

    decision = await service.auto_play_request(1, 2, "Kalyani")

    assert decision.action == "play_match"
    assert decision.track == candidates[1]
    assert decision.source.kind == "lavalink"


@pytest.mark.asyncio
async def test_youtube_auto_play_explains_when_all_ranked_results_fail_to_load(database):
    candidates = [Track("youtube", "first-video", "Kalyani", "Official Channel")]
    provider = FakeYouTubeProvider(candidates)
    provider.resolve_errors.add("first-video")
    service = SongMemoryService(database, ProviderRegistry([provider]))

    decision = await service.auto_play_request(1, 2, "Kalyani")

    assert decision.action == "search"
    assert decision.track is None
    assert "could not load playable audio" in decision.search.warnings[-1]
