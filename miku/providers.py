from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import aiohttp
import wavelink

from .models import PlayableSource, Track

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class MusicProvider(ABC):
    name: str
    playback_search = False

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[Track]: ...

    @abstractmethod
    async def resolve(self, track: Track) -> PlayableSource | None: ...

    @abstractmethod
    def supports_playback(self, track: Track) -> bool: ...

    async def cleanup(self) -> None:
        return None


class LocalLibraryProvider(MusicProvider):
    name = "local"
    playback_search = True
    extensions = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".opus"}

    def __init__(self, music_dir: str | Path):
        self.root = Path(music_dir).expanduser().resolve()

    def _safe_path(self, stable_id: str) -> Path | None:
        candidate = Path(stable_id)
        if candidate.is_absolute():
            return None
        try:
            resolved = (self.root / candidate).resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError):
            return None
        if not resolved.is_file() or resolved.suffix.lower() not in self.extensions:
            return None
        return resolved

    @staticmethod
    def _metadata(path: Path) -> tuple[str, str | None, int | None]:
        title, artist, duration = path.stem, None, None
        try:
            from tinytag import TinyTag
            audio = TinyTag.get(str(path))
            if audio:
                if audio.title:
                    title = str(audio.title)
                if audio.artist:
                    artist = str(audio.artist)
                if audio.duration:
                    duration = int(audio.duration)
        except Exception:
            pass
        return title, artist, duration

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        if not query.strip() or len(query) > 200 or not self.root.exists():
            return []
        terms = query.casefold().split()
        candidates: list[Track] = []
        for path in self.root.rglob("*"):
            if path.suffix.lower() not in self.extensions:
                continue
            safe = self._safe_path(path.relative_to(self.root).as_posix())
            if safe is None:
                continue
            title, artist, duration = self._metadata(safe)
            haystack = f"{title} {artist or ''} {safe.stem}".casefold()
            if all(term in haystack for term in terms):
                candidates.append(Track(
                    provider=self.name,
                    stable_id=safe.relative_to(self.root).as_posix(),
                    title=title,
                    artist=artist,
                    duration_seconds=duration,
                    source_url=None,
                ))
        return candidates[: max(0, min(limit, 5))]

    async def resolve(self, track: Track) -> PlayableSource | None:
        if track.provider != self.name:
            return None
        path = self._safe_path(track.stable_id)
        return PlayableSource("file", str(path)) if path else None

    def supports_playback(self, track: Track) -> bool:
        return track.provider == self.name and self._safe_path(track.stable_id) is not None


def valid_thumbnail_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        if parsed.hostname != "ytimg.com" and not parsed.hostname.endswith(".ytimg.com"):
            return None
        return value
    except ValueError:
        return None


def _duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class YouTubeSearchProvider(MusicProvider):
    """Search public YouTube metadata and resolve playback through an authorized Lavalink node."""

    name = "youtube"
    playback_search = True
    API_ROOT = "https://www.googleapis.com/youtube/v3"
    _video_id_pattern = re.compile(r"[A-Za-z0-9_-]{11}\Z")
    _youtube_hosts = {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
        "youtu.be", "www.youtu.be",
    }

    def __init__(self, api_key: str | None, timeout_seconds: float = 12, *,
                 playback_enabled: bool = False):
        self._api_key = api_key.strip() if api_key else ""
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.playback_enabled = playback_enabled

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @classmethod
    def is_youtube_url(cls, value: str) -> bool:
        try:
            parsed = urlparse(value.strip())
            port = parsed.port
        except ValueError:
            return False
        return bool(
            parsed.scheme == "https" and parsed.hostname in cls._youtube_hosts
            and port in (None, 443) and not parsed.username and not parsed.password
            and not parsed.fragment
        )

    @classmethod
    def video_id_from_url(cls, value: str) -> str | None:
        if not cls.is_youtube_url(value):
            return None
        parsed = urlparse(value.strip())
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.strip("/").split("/", 1)[0]
        else:
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            candidate = query.get("v", "")
            if not candidate:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
                    candidate = parts[1]
        return candidate if cls._video_id_pattern.fullmatch(candidate) else None

    @staticmethod
    def _playable_results(result) -> list:
        if isinstance(result, wavelink.Playlist):
            return list(result.tracks)
        return list(result or ())

    @classmethod
    def _track_from_playable(cls, playable) -> Track | None:
        if str(getattr(playable, "source", "")).casefold() != "youtube":
            return None
        video_id = str(getattr(playable, "identifier", "") or "")
        if not cls._video_id_pattern.fullmatch(video_id):
            video_id = cls.video_id_from_url(str(getattr(playable, "uri", "") or "")) or ""
        if not video_id:
            return None
        try:
            length = int(getattr(playable, "length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        uri = getattr(playable, "uri", None)
        source_url = uri if isinstance(uri, str) and cls.is_youtube_url(uri) else (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        artwork = getattr(playable, "artwork", None)
        if not (isinstance(artwork, str) and artwork.startswith("https://")):
            artwork = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        return Track(
            provider=cls.name,
            stable_id=video_id,
            title=str(getattr(playable, "title", "") or "Untitled video"),
            artist=str(getattr(playable, "author", "") or "Unknown channel"),
            duration_seconds=length // 1000 if length > 0 else None,
            thumbnail_url=valid_thumbnail_url(artwork),
            source_url=source_url,
        )

    async def _lavalink_search(self, query: str, limit: int = 5) -> list[Track]:
        if not self.playback_enabled:
            raise ProviderError("YouTube search is not configured (YOUTUBE_API_KEY is empty).")
        try:
            result = await wavelink.Playable.search(query, source="ytsearch")
        except Exception as exc:
            log.warning("Lavalink YouTube search failed (%s)", type(exc).__name__)
            raise ProviderError("Lavalink YouTube search is temporarily unavailable.") from None
        return [
            track for playable in self._playable_results(result)[:max(1, min(limit, 5))]
            if (track := self._track_from_playable(playable)) is not None
        ]

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        if not query.strip() or len(query) > 200:
            raise ProviderError("Enter a song query between 1 and 200 characters.")
        if not self.configured:
            return await self._lavalink_search(query, limit)
        limit = max(1, min(limit, 5))
        params = {"part": "snippet", "type": "video", "maxResults": str(limit),
                  "q": query, "key": self._api_key}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(f"{self.API_ROOT}/search", params=params) as response:
                    body = await response.json(content_type=None)
                    if response.status == 403:
                        reason = body.get("error", {}).get("errors", [{}])[0].get("reason")
                        if reason in {"quotaExceeded", "dailyLimitExceeded"}:
                            raise ProviderError("YouTube API quota is exhausted.")
                    if response.status >= 400:
                        raise ProviderError("YouTube search request failed.")
                ids = [item.get("id", {}).get("videoId") for item in body.get("items", [])]
                ids = [video_id for video_id in ids
                       if isinstance(video_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)]
                if not ids:
                    return []
                details_params = {"part": "contentDetails,status", "id": ",".join(ids),
                                  "key": self._api_key}
                async with session.get(f"{self.API_ROOT}/videos", params=details_params) as response:
                    details = await response.json(content_type=None)
                    if response.status >= 400:
                        raise ProviderError("YouTube video details request failed.")
        except ProviderError:
            if self.playback_enabled:
                return await self._lavalink_search(query, limit)
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            if self.playback_enabled:
                return await self._lavalink_search(query, limit)
            raise ProviderError("YouTube search is temporarily unavailable.") from None

        available = {
            item.get("id"): item for item in details.get("items", [])
            if item.get("status", {}).get("privacyStatus", "public") == "public"
        }
        by_id = {item.get("id", {}).get("videoId"): item for item in body.get("items", [])}
        tracks: list[Track] = []
        for video_id in ids:
            item = by_id.get(video_id)
            if not item or video_id not in available:
                continue
            snippet = item.get("snippet", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = next((valid_thumbnail_url(thumbnails.get(size, {}).get("url"))
                              for size in ("high", "medium", "default")
                              if thumbnails.get(size, {}).get("url")), None)
            duration = _duration_seconds(
                available[video_id].get("contentDetails", {}).get("duration")
            )
            tracks.append(Track(
                provider=self.name,
                stable_id=video_id,
                title=str(snippet.get("title") or "Untitled video"),
                artist=str(snippet.get("channelTitle") or "Unknown channel"),
                duration_seconds=duration,
                thumbnail_url=thumbnail,
                source_url=f"https://www.youtube.com/watch?v={video_id}",
            ))
        return tracks

    async def resolve(self, track: Track) -> PlayableSource | None:
        if not self.supports_playback(track) or not self.is_youtube_url(track.source_url or ""):
            return None
        try:
            result = await wavelink.Playable.search(track.source_url)
        except Exception as exc:
            log.warning(
                "Lavalink failed to load YouTube video %s (%s)",
                track.stable_id, type(exc).__name__,
            )
            raise ProviderError("This YouTube video could not be loaded by the configured audio source.") from None
        exact = next((candidate for candidate in self._playable_results(result)
                      if str(getattr(candidate, "source", "")).casefold() == "youtube"
                      and str(getattr(candidate, "identifier", "")) == track.stable_id), None)
        if exact is None:
            raise ProviderError(
                "This YouTube video is unavailable to the configured audio source."
            )
        return PlayableSource("lavalink", exact)

    async def resolve_url(self, value: str) -> list[tuple[Track, PlayableSource]]:
        if not self.is_youtube_url(value):
            return []
        if not self.playback_enabled:
            raise ProviderError(
                "YouTube playback requires LAVALINK_URI, LAVALINK_PASSWORD, and a Lavalink v4 server "
                "with the YouTube source plugin."
            )
        try:
            result = await wavelink.Playable.search(value.strip())
        except Exception as exc:
            log.warning("Lavalink failed to load a YouTube URL (%s)", type(exc).__name__)
            raise ProviderError(
                "That YouTube video or playlist could not be loaded by Lavalink. Check the server and try again."
            ) from None
        playables = [
            playable for playable in self._playable_results(result)
            if str(getattr(playable, "source", "")).casefold() == "youtube"
        ]
        if not playables:
            raise ProviderError(
                "That YouTube video or playlist is unavailable, restricted, or unsupported by the audio server."
            )
        requested_video_id = self.video_id_from_url(value)
        # If a playlist URL also selects a video, keep that video first while retaining the playlist.
        if requested_video_id:
            selected = next((item for item in playables
                             if str(getattr(item, "identifier", "")) == requested_video_id), None)
            if selected is None:
                raise ProviderError(
                    "Lavalink did not return the video linked in that URL. Miku stopped to avoid playing a different video."
                )
            # URLs containing both a video and a playlist should begin with the exact linked video.
            playables = [selected, *(item for item in playables if item is not selected)]
        resolved: list[tuple[Track, PlayableSource]] = []
        for playable in playables[:500]:
            track = self._track_from_playable(playable)
            if track is not None:
                resolved.append((track, PlayableSource("lavalink", playable)))
        if not resolved:
            raise ProviderError(
                "That YouTube video or playlist did not contain playable audio tracks."
            )
        return resolved

    def supports_playback(self, track: Track) -> bool:
        return bool(
            self.playback_enabled and track.provider == self.name
            and self._video_id_pattern.fullmatch(track.stable_id)
            and self.is_youtube_url(track.source_url or "")
        )


class DisabledAuthorizedStreamProvider(MusicProvider):
    """No provider endpoint is assumed. Replace with a reviewed licensed adapter."""

    name = "authorized_stream"

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        return []

    async def resolve(self, track: Track) -> PlayableSource | None:
        return None

    def supports_playback(self, track: Track) -> bool:
        return False


class JamendoProvider(MusicProvider):
    """Jamendo API search and licensed stream URLs; never downloads or caches audio."""

    name = "jamendo"
    playback_search = True
    API_ROOT = "https://api.jamendo.com/v3.0/tracks/"
    _track_id_pattern = re.compile(r"\d{1,12}\Z")

    def __init__(self, client_id: str | None, timeout_seconds: float = 12):
        self._client_id = client_id.strip() if client_id else ""
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    @property
    def configured(self) -> bool:
        return bool(self._client_id)

    @classmethod
    def track_id_from_url(cls, value: str) -> str | None:
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme != "https" or port not in (None, 443)
                or parsed.username or parsed.password
                or parsed.hostname not in {"jamendo.com", "www.jamendo.com", "jamen.do"}):
            return None
        patterns = (
            r"^/track/(\d+)(?:/[^/]*)?/?$",
            r"^/t/(\d+)/?$",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, parsed.path)
            if match and cls._track_id_pattern.fullmatch(match.group(1)):
                return match.group(1)
        return None

    @staticmethod
    def _playback_license_allowed(value: str | None) -> bool:
        if not value:
            return False
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        if parsed.hostname != "creativecommons.org":
            return False
        path = parsed.path.casefold()
        # The Discord voice output is transcoded by FFmpeg, so exclude NoDerivatives.
        return bool(re.match(r"^/licenses/by(?:-nc)?(?:-sa)?/\d+(?:\.\d+)?/?$", path)
                    or re.match(r"^/publicdomain/zero/\d+(?:\.\d+)?/?$", path))

    @staticmethod
    def _safe_audio_url(value: str | None) -> str | None:
        if not value:
            return None
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        hostname = (parsed.hostname or "").casefold()
        try:
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme != "https" or port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment
                or not (hostname == "storage.jamendo.com"
                        or hostname.endswith(".storage.jamendo.com"))):
            return None
        return value

    @staticmethod
    def _to_track(item: dict) -> Track | None:
        stable_id = str(item.get("id") or "")
        if not re.fullmatch(r"\d{1,12}", stable_id):
            return None
        try:
            duration = int(item.get("duration")) if item.get("duration") is not None else None
        except (TypeError, ValueError):
            duration = None
        thumbnail = item.get("album_image") or item.get("image")
        try:
            parsed_thumbnail = urlparse(thumbnail) if isinstance(thumbnail, str) else None
            thumbnail_host = parsed_thumbnail.hostname if parsed_thumbnail else None
            thumbnail_port = parsed_thumbnail.port if parsed_thumbnail else None
        except ValueError:
            parsed_thumbnail = None
            thumbnail_host = None
            thumbnail_port = None
        if (not isinstance(thumbnail, str) or not thumbnail.startswith("https://")
                or thumbnail_host != "usercontent.jamendo.com"
                or thumbnail_port not in (None, 443)
                or parsed_thumbnail.username or parsed_thumbnail.password
                or parsed_thumbnail.fragment):
            thumbnail = None
        source_url = f"https://www.jamendo.com/track/{stable_id}"
        return Track(
            provider="jamendo",
            stable_id=stable_id,
            title=str(item.get("name") or "Untitled track"),
            artist=str(item.get("artist_name") or "Unknown artist"),
            duration_seconds=duration,
            thumbnail_url=thumbnail,
            source_url=source_url,
            album=(str(item["album_name"]) if item.get("album_name") else None),
            release_date=(str(item["releasedate"]) if item.get("releasedate") else None),
            license_url=(str(item["license_ccurl"]) if item.get("license_ccurl") else None),
            official_source=None,
        )

    async def _request_tracks(self, params: dict[str, str]) -> list[dict]:
        if not self.configured:
            raise ProviderError("Jamendo is not configured (JAMENDO_CLIENT_ID is empty).")
        request_params = {
            "client_id": self._client_id,
            "format": "json",
            "limit": "5",
            "include": "licenses",
            **params,
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(self.API_ROOT, params=request_params) as response:
                    payload = await response.json(content_type=None)
                    if response.status in (401, 403):
                        raise ProviderError("Jamendo rejected the configured client ID.")
                    if response.status == 429:
                        raise ProviderError("Jamendo API rate limit was reached; try again later.")
                    if response.status >= 500:
                        raise ProviderError("Jamendo is temporarily unavailable.")
                    if response.status >= 400:
                        raise ProviderError("Jamendo search request failed.")
        except ProviderError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise ProviderError("Jamendo is temporarily unavailable.") from None
        headers = payload.get("headers", {}) if isinstance(payload, dict) else {}
        if headers.get("status") not in (None, "success"):
            raise ProviderError("Jamendo could not complete the catalog request.")
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        if not query.strip() or len(query) > 200:
            raise ProviderError("Enter a song query between 1 and 200 characters.")
        requested = max(1, min(limit, 5))
        rows = await self._request_tracks({"search": query, "limit": str(requested)})
        tracks = (self._to_track(row) for row in rows)
        return [track for track in tracks if track is not None]

    async def search_playback(self, query: str, limit: int = 5) -> list[Track]:
        """Search beyond ineligible items so manual selection contains playable tracks only."""
        if not query.strip() or len(query) > 200:
            raise ProviderError("Enter a song query between 1 and 200 characters.")
        requested = max(1, min(limit, 5))
        rows = await self._request_tracks({"search": query, "limit": str(min(20, requested * 4))})
        tracks = (self._to_track(row) for row in rows)
        return [
            track for track in tracks
            if track is not None and self.supports_playback(track)
        ][:requested]

    async def track_from_url(self, value: str) -> Track | None:
        track_id = self.track_id_from_url(value)
        if track_id is None:
            return None
        rows = await self._request_tracks({"id": track_id, "audioformat": "mp31"})
        return next((track for row in rows if (track := self._to_track(row)) is not None), None)

    async def resolve(self, track: Track) -> PlayableSource | None:
        if not self.supports_playback(track):
            return None
        rows = await self._request_tracks({
            "id": track.stable_id,
            "audioformat": "mp31",
            "limit": "1",
        })
        for row in rows:
            fresh = self._to_track(row)
            if fresh is None or fresh.key != track.key:
                continue
            if not self._playback_license_allowed(fresh.license_url):
                return None
            audio_url = self._safe_audio_url(row.get("audio"))
            return PlayableSource("http", audio_url) if audio_url else None
        return None

    def supports_playback(self, track: Track) -> bool:
        return bool(
            self.configured
            and track.provider == self.name
            and self._track_id_pattern.fullmatch(track.stable_id)
            and self._playback_license_allowed(track.license_url)
        )


class ProviderRegistry:
    def __init__(self, providers: list[MusicProvider]):
        self.providers = {provider.name: provider for provider in providers}

    async def search(self, query: str, limit: int = 5, *,
                     playback_only: bool = False) -> tuple[list[Track], list[str]]:
        limit = max(1, min(limit, 5))
        selected = [
            provider for provider in self.providers.values()
            if not playback_only or provider.playback_search
        ]
        results = await asyncio.gather(
            *((getattr(provider, "search_playback", provider.search)(query, limit)
               if playback_only else provider.search(query, limit)) for provider in selected),
            return_exceptions=True,
        )
        per_provider: list[list[Track]] = []
        warnings: list[str] = []
        for provider, result in zip(selected, results):
            if isinstance(result, ProviderError):
                warnings.append(str(result))
                per_provider.append([])
            elif isinstance(result, Exception):
                log.exception(
                    "Unexpected search failure from music provider %s",
                    provider.name,
                    exc_info=(type(result), result, result.__traceback__),
                )
                warnings.append(f"{provider.name} search is temporarily unavailable.")
                per_provider.append([])
            else:
                per_provider.append(result[:limit])

        # Interleave sources so an early provider cannot starve other catalogs.
        tracks: list[Track] = []
        seen: set[tuple[str, str]] = set()
        for index in range(limit):
            for provider_tracks in per_provider:
                if index >= len(provider_tracks):
                    continue
                track = provider_tracks[index]
                if track.key not in seen:
                    tracks.append(track)
                    seen.add(track.key)
                    if len(tracks) >= limit:
                        return tracks, warnings
        return tracks, warnings

    async def resolve(self, track: Track) -> PlayableSource | None:
        provider = self.providers.get(track.provider)
        if not provider or not provider.supports_playback(track):
            return None
        return await provider.resolve(track)

    def supports_playback(self, track: Track) -> bool:
        provider = self.providers.get(track.provider)
        return bool(provider and provider.supports_playback(track))

    async def resolve_url(self, value: str) -> Track | None:
        for provider in self.providers.values():
            resolver = getattr(provider, "track_from_url", None)
            if resolver is not None:
                track = await resolver(value)
                if track is not None:
                    return track
        return None

    async def cleanup(self) -> None:
        await asyncio.gather(*(provider.cleanup() for provider in self.providers.values()))
