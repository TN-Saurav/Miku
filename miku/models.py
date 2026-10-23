from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Track:
    """Stable, public track metadata; never contains a temporary stream URL."""

    provider: str
    stable_id: str
    title: str
    artist: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    source_url: str | None = None
    album: str | None = None
    release_date: str | None = None
    license_url: str | None = None
    official_source: bool | None = None
    requester: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.stable_id

    def to_dict(self) -> dict[str, Any]:
        # Requester identity is transient player state, not catalog or preference data.
        data = asdict(self)
        data.pop("requester", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Track":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class PlayableSource:
    """A source resolved just before playback, never persisted in song memory."""

    kind: str
    value: Any


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    tracks: tuple[Track, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlayDecision:
    action: str  # "play_match" or "search"
    track: Track | None = None
    notice: str | None = None
    search: SearchOutcome | None = None
    source: PlayableSource | None = None
