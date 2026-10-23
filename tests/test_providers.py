import pytest
import wavelink
from unittest.mock import AsyncMock, call

from miku.models import Track
from miku.providers import (
    JamendoProvider,
    LocalLibraryProvider,
    ProviderError,
    ProviderRegistry,
    YouTubeSearchProvider,
    _duration_seconds,
    valid_thumbnail_url,
)


def _lavalink_track(video_id="AbCdEfGhI_1", title="Video title", uri=None):
    return wavelink.Playable({
        "encoded": f"encoded-{video_id}",
        "info": {
            "identifier": video_id,
            "isSeekable": True,
            "author": "Example channel",
            "length": 187_000,
            "isStream": False,
            "position": 0,
            "title": title,
            "uri": uri or f"https://www.youtube.com/watch?v={video_id}",
            "artworkUrl": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "sourceName": "youtube",
        },
        "pluginInfo": {},
    })


def test_thumbnail_urls_are_https_youtube_image_hosts_only():
    assert valid_thumbnail_url("https://i.ytimg.com/vi/abc/default.jpg")
    assert valid_thumbnail_url("http://i.ytimg.com/vi/abc/default.jpg") is None
    assert valid_thumbnail_url("https://example.com/image.jpg") is None
    assert valid_thumbnail_url(None) is None


def test_iso_duration_parser_handles_missing_and_bad_values():
    assert _duration_seconds("PT4M5S") == 245
    assert _duration_seconds("PT1H2M3S") == 3723
    assert _duration_seconds("PT5S") == 5
    assert _duration_seconds(None) is None
    assert _duration_seconds("not-a-duration") is None


@pytest.mark.asyncio
async def test_youtube_without_key_fails_clearly_and_never_resolves_audio():
    provider = YouTubeSearchProvider("")
    with pytest.raises(ProviderError, match="not configured"):
        await provider.search("Kalyani")
    track = Track("youtube", "video-id", "Title", source_url="https://www.youtube.com/watch?v=video-id")
    assert provider.supports_playback(track) is False
    assert await provider.resolve(track) is None


@pytest.mark.asyncio
async def test_youtube_errors_are_surface_safe_provider_warnings(monkeypatch):
    class Response:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"error": {"message": "secret-key must not be shown"}}

    class Session:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    provider = YouTubeSearchProvider("secret-key")
    outcome_tracks, warnings = await ProviderRegistry([provider]).search("Kalyani")
    assert outcome_tracks == []
    assert warnings == ["YouTube search request failed."]
    assert "secret-key" not in " ".join(warnings)


@pytest.mark.asyncio
async def test_youtube_quota_exhaustion_is_distinguished(monkeypatch):
    class Response:
        status = 403

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"error": {"errors": [{"reason": "quotaExceeded"}]}}

    class Session:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    with pytest.raises(ProviderError, match="quota is exhausted"):
        await YouTubeSearchProvider("not-logged").search("Kalyani")


@pytest.mark.asyncio
async def test_youtube_empty_results_do_not_request_video_details(monkeypatch):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"items": []}

    class Session:
        def __init__(self, timeout=None):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            self.calls.append(url)
            return Response()

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    assert await YouTubeSearchProvider("api-key").search("missing song") == []


@pytest.mark.asyncio
async def test_youtube_metadata_uses_api_thumbnail_duration_and_canonical_url(monkeypatch):
    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return self.payload

    class Session:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, **kwargs):
            if url.endswith("/search"):
                return Response({"items": [{
                    "id": {"videoId": "AbCdEfGhI_1"},
                    "snippet": {"title": "Kalyani", "channelTitle": "Channel",
                                "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/x/mq.jpg"}}},
                }]})
            return Response({"items": [{"id": "AbCdEfGhI_1", "status": {"privacyStatus": "public"},
                                         "contentDetails": {"duration": "PT3M7S"}}]})

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    results = await YouTubeSearchProvider("api-key").search("Kalyani")
    assert len(results) == 1
    assert results[0].title == "Kalyani"
    assert results[0].artist == "Channel"
    assert results[0].duration_seconds == 187
    assert results[0].thumbnail_url == "https://i.ytimg.com/vi/x/mq.jpg"
    assert results[0].source_url == "https://www.youtube.com/watch?v=AbCdEfGhI_1"


@pytest.mark.asyncio
async def test_local_library_search_and_path_traversal_guard(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Kalyani.mp3").write_bytes(b"not real audio")
    outside = tmp_path / "private.mp3"
    outside.write_bytes(b"private")
    provider = LocalLibraryProvider(root)

    results = await provider.search("Kalyani")
    assert len(results) == 1
    assert results[0].stable_id == "Artist - Kalyani.mp3"
    assert str(tmp_path) not in str(results[0].to_dict())
    assert await provider.resolve(results[0]) is not None

    traversal = Track("local", "../private.mp3", "Private")
    assert provider.supports_playback(traversal) is False
    assert await provider.resolve(traversal) is None


@pytest.mark.asyncio
async def test_local_provider_returns_no_match_for_empty_or_missing_library(tmp_path):
    provider = LocalLibraryProvider(tmp_path / "does-not-exist")
    assert await provider.search("song") == []


def _jamendo_row(track_id="12345", license_url="https://creativecommons.org/licenses/by/4.0/"):
    return {
        "id": track_id,
        "name": "Indie Song",
        "artist_name": "Independent Artist",
        "album_name": "First EP",
        "duration": "187",
        "releasedate": "2025-04-01",
        "album_image": "https://usercontent.jamendo.com?type=album&id=7&width=300",
        "shareurl": f"https://www.jamendo.com/track/{track_id}/indie-song",
        "license_ccurl": license_url,
        "audio": f"https://prod-1.storage.jamendo.com/?trackid={track_id}&format=mp31",
    }


@pytest.mark.asyncio
async def test_jamendo_search_maps_metadata_and_playback_filters_ineligible_licenses(monkeypatch):
    class Response:
        status = 200

        def __init__(self, rows):
            self.rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"headers": {"status": "success"}, "results": self.rows}

    class Session:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, *, params):
            assert url == JamendoProvider.API_ROOT
            assert params["include"] == "licenses"
            return Response([
                _jamendo_row("12345"),
                _jamendo_row("67890", "https://creativecommons.org/licenses/by-nc-nd/3.0/"),
            ])

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    provider = JamendoProvider("test-client-id")

    metadata = await provider.search("Indie Song", limit=2)
    playable = await provider.search_playback("Indie Song", limit=2)

    assert [track.stable_id for track in metadata] == ["12345", "67890"]
    assert metadata[0].artist == "Independent Artist"
    assert metadata[0].album == "First EP"
    assert metadata[0].duration_seconds == 187
    assert metadata[0].thumbnail_url.startswith("https://usercontent.jamendo.com")
    assert metadata[0].source_url.startswith("https://www.jamendo.com/track/12345")
    assert [track.stable_id for track in playable] == ["12345"]
    assert provider.supports_playback(metadata[1]) is False


@pytest.mark.asyncio
async def test_jamendo_exact_link_resolves_only_documented_https_stream(monkeypatch):
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"headers": {"status": "success"}, "results": [_jamendo_row()]}

    class Session:
        def __init__(self, timeout=None):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, *, params):
            calls.append(params)
            return Response()

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    provider = JamendoProvider("private-test-client")
    assert JamendoProvider.track_id_from_url("https://www.jamendo.com/track/12345/indie-song") == "12345"
    assert JamendoProvider.track_id_from_url("https://jamen.do/t/12345") == "12345"
    assert JamendoProvider.track_id_from_url("https://example.com/track/12345") is None

    track = await provider.track_from_url("https://www.jamendo.com/track/12345/indie-song")
    source = await provider.resolve(track)

    assert track.stable_id == "12345"
    assert source.kind == "http"
    assert source.value == "https://prod-1.storage.jamendo.com/?trackid=12345&format=mp31"
    assert "audio" not in track.to_dict()
    assert calls and all(params["id"] == "12345" for params in calls)


@pytest.mark.asyncio
async def test_jamendo_api_failures_never_return_credentials_or_raw_provider_errors(monkeypatch):
    class Response:
        status = 403

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"error": "private credential detail"}

    class Session:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    provider = JamendoProvider("private-client-value")
    tracks, warnings = await ProviderRegistry([provider]).search("Kalyani")

    assert tracks == []
    assert warnings == ["Jamendo rejected the configured client ID."]
    assert "private-client-value" not in " ".join(warnings)
    assert "private credential detail" not in " ".join(warnings)


@pytest.mark.asyncio
async def test_jamendo_rate_limit_is_reported_with_safe_actionable_warning(monkeypatch):
    class Response:
        status = 429

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, content_type=None):
            return {"detail": "private provider response"}

    class Session:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("miku.providers.aiohttp.ClientSession", Session)
    tracks, warnings = await ProviderRegistry([JamendoProvider("test-id")]).search("song")

    assert tracks == []
    assert warnings == ["Jamendo API rate limit was reached; try again later."]
    assert "private provider response" not in " ".join(warnings)


def test_youtube_url_validation_accepts_supported_hosts_and_video_paths():
    assert YouTubeSearchProvider.is_youtube_url("https://youtu.be/AbCdEfGhI_1?t=5")
    assert YouTubeSearchProvider.video_id_from_url(
        "https://www.youtube.com/shorts/AbCdEfGhI_1"
    ) == "AbCdEfGhI_1"
    assert YouTubeSearchProvider.video_id_from_url(
        "https://music.youtube.com/watch?v=AbCdEfGhI_1"
    ) == "AbCdEfGhI_1"
    assert not YouTubeSearchProvider.is_youtube_url(
        "https://youtube.com.example.invalid/watch?v=AbCdEfGhI_1"
    )
    assert not YouTubeSearchProvider.is_youtube_url(
        "https://www.youtube.com:8443/watch?v=AbCdEfGhI_1"
    )


@pytest.mark.asyncio
async def test_youtube_search_falls_back_to_lavalink_when_api_key_is_empty(monkeypatch):
    playable = _lavalink_track()
    search = AsyncMock(return_value=[playable])
    monkeypatch.setattr(wavelink.Playable, "search", search)
    tracks = await YouTubeSearchProvider("", playback_enabled=True).search("Video title")

    assert len(tracks) == 1
    assert tracks[0].provider == "youtube"
    assert tracks[0].stable_id == "AbCdEfGhI_1"
    assert tracks[0].duration_seconds == 187
    assert tracks[0].artist == "Example channel"
    search.assert_awaited_once_with("Video title", source="ytsearch")


@pytest.mark.asyncio
async def test_youtube_resolution_uses_the_exact_video_and_direct_playlist(monkeypatch):
    video = _lavalink_track()
    later = _lavalink_track("ZyXwVuTsRq0", "Second video")
    provider = YouTubeSearchProvider("", playback_enabled=True)
    search = AsyncMock(return_value=[video, later])
    monkeypatch.setattr(wavelink.Playable, "search", search)

    track = Track(
        "youtube", "AbCdEfGhI_1", "Metadata title", "Channel",
        source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1",
    )
    source = await provider.resolve(track)
    playlist = await provider.resolve_url("https://youtu.be/AbCdEfGhI_1?list=PLexample")

    assert source is not None and source.kind == "lavalink" and source.value is video
    assert [item[0].stable_id for item in playlist] == ["AbCdEfGhI_1", "ZyXwVuTsRq0"]
    assert all(item[1].kind == "lavalink" for item in playlist)
    assert search.await_args_list == [
        call(track.source_url),
        call("https://youtu.be/AbCdEfGhI_1?list=PLexample"),
    ]


@pytest.mark.asyncio
async def test_direct_youtube_url_rejects_a_different_lavalink_playable(monkeypatch):
    wrong_video = _lavalink_track("ZyXwVuTsRq0", "Different video")
    monkeypatch.setattr(wavelink.Playable, "search", AsyncMock(return_value=[wrong_video]))

    with pytest.raises(ProviderError, match="did not return the video linked"):
        await YouTubeSearchProvider("", playback_enabled=True).resolve_url(
            "https://www.youtube.com/watch?v=AbCdEfGhI_1"
        )


@pytest.mark.asyncio
async def test_youtube_metadata_mapping_rejects_non_youtube_audio_source():
    other_source = wavelink.Playable({
        "encoded": "encoded-other",
        "info": {
            "identifier": "AbCdEfGhI_1", "isSeekable": True, "author": "Uploader",
            "length": 10_000, "isStream": False, "position": 0, "title": "Other source",
            "uri": "https://example.invalid/watch/AbCdEfGhI_1",
            "sourceName": "bandcamp",
        },
        "pluginInfo": {},
    })

    assert YouTubeSearchProvider._track_from_playable(other_source) is None


@pytest.mark.asyncio
async def test_youtube_plugin_playlist_payload_maps_all_loaded_tracks(monkeypatch):
    first = _lavalink_track("AbCdEfGhI_1", "First")
    second = _lavalink_track("ZyXwVuTsRq0", "Second")
    playlist = wavelink.Playlist({
        "info": {"name": "Example playlist", "selectedTrack": 1},
        "pluginInfo": {
            "type": "playlist", "url": "https://www.youtube.com/playlist?list=PLexample",
            "artworkUrl": None, "author": "Channel",
        },
        "tracks": [first.raw_data, second.raw_data],
    })
    monkeypatch.setattr(wavelink.Playable, "search", AsyncMock(return_value=playlist))

    resolved = await YouTubeSearchProvider("", playback_enabled=True).resolve_url(
        "https://www.youtube.com/playlist?list=PLexample"
    )

    assert [track.title for track, _ in resolved] == ["First", "Second"]
    assert all(source.kind == "lavalink" for _, source in resolved)


@pytest.mark.asyncio
async def test_youtube_resolution_reports_restricted_video_without_provider_details(monkeypatch):
    provider = YouTubeSearchProvider("", playback_enabled=True)
    async def fail(*args, **kwargs):
        raise RuntimeError("password=private-value")

    monkeypatch.setattr(wavelink.Playable, "search", fail)
    with pytest.raises(ProviderError, match="could not be loaded") as exc:
        await provider.resolve_url("https://www.youtube.com/watch?v=AbCdEfGhI_1")
    assert "private-value" not in str(exc.value)
