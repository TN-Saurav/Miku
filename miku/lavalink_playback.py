from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import discord
import wavelink

from .config import Settings
from .models import PlayableSource, Track
from .playback import PlaybackManager, PlayerControlsView, format_duration, track_embed
from .providers import YouTubeSearchProvider

log = logging.getLogger(__name__)

YOUTUBE_SETUP_FEEDBACK = (
    "YouTube audio needs a reachable Lavalink v4 server with the maintained YouTube source plugin. "
    "Configure `LAVALINK_URI` and `LAVALINK_PASSWORD` in `.env`, then restart Miku."
)

_SECRET_DETAIL_PATTERN = re.compile(
    r"(?i)\b(password|token|authorization|cookie|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|secret)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_URL_DETAIL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\])}>,]+")


def safe_log_detail(
    value: Any, *, limit: int | None = 500, flatten: bool = True,
) -> str:
    """Keep useful upstream failure details while removing secrets and connection URLs."""
    detail = str(value or "")
    if flatten:
        detail = " ".join(detail.split())
    else:
        detail = detail.replace("\r\n", "\n").replace("\r", "\n")
    detail = _URL_DETAIL_PATTERN.sub("[URL]", detail)
    detail = _SECRET_DETAIL_PATTERN.sub(r"\1\2[REDACTED]", detail)
    return detail if limit is None else detail[:limit]


@dataclass(frozen=True, slots=True)
class _QueuedTrack:
    track: Track
    playable: wavelink.Playable
    token: str


@dataclass(frozen=True, slots=True)
class _DeferredTrack:
    track: Track
    source: PlayableSource | None
    voice_channel: Any
    text_channel: Any


class LavalinkGuildQueue:
    """Queue metadata and its matching Lavalink playable, isolated per guild."""

    def __init__(self):
        self.current_item: _QueuedTrack | None = None
        self.upcoming: deque[_QueuedTrack] = deque()
        self.history: deque[_QueuedTrack] = deque(maxlen=100)
        self._lock = asyncio.Lock()

    @property
    def current(self) -> Track | None:
        return self.current_item.track if self.current_item else None

    async def enqueue_many(self, items: list[_QueuedTrack]) -> int:
        async with self._lock:
            start = len(self.upcoming) + 1
            self.upcoming.extend(items)
            return start

    async def enqueue_front(self, item: _QueuedTrack) -> int:
        async with self._lock:
            self.upcoming.appendleft(item)
            return len(self.upcoming)

    async def start_next(self) -> _QueuedTrack | None:
        async with self._lock:
            if self.current_item is not None:
                self.history.append(self.current_item)
            item = self.upcoming.popleft() if self.upcoming else None
            if item is not None:
                token = secrets.token_hex(12)
                item.playable.extras = {"miku_playback_token": token}
                item = _QueuedTrack(item.track, item.playable, token)
            self.current_item = item
            return self.current_item

    async def finish_current(self) -> _QueuedTrack | None:
        async with self._lock:
            current = self.current_item
            if current is not None:
                self.history.append(current)
                self.current_item = None
            return current

    async def prepare_previous(self) -> bool:
        async with self._lock:
            if not self.history:
                return False
            previous = self.history.pop()
            if self.current_item is not None:
                self.upcoming.appendleft(self.current_item)
            self.upcoming.appendleft(previous)
            return True

    async def snapshot(self) -> tuple[Track | None, tuple[Track, ...]]:
        async with self._lock:
            return self.current, tuple(item.track for item in self.upcoming)

    async def remove(self, position: int) -> Track:
        async with self._lock:
            if position < 1 or position > len(self.upcoming):
                raise IndexError("queue position is out of range")
            items = list(self.upcoming)
            removed = items.pop(position - 1)
            self.upcoming = deque(items)
            return removed.track

    async def clear_upcoming(self) -> int:
        async with self._lock:
            count = len(self.upcoming)
            self.upcoming.clear()
            return count

    async def shuffle(self) -> int:
        async with self._lock:
            items = list(self.upcoming)
            random.shuffle(items)
            self.upcoming = deque(items)
            return len(items)

    async def clear_all(self) -> None:
        async with self._lock:
            self.upcoming.clear()
            self.current_item = None
            self.history.clear()


@dataclass
class LavalinkGuildState:
    queue: LavalinkGuildQueue = field(default_factory=LavalinkGuildQueue)
    player: wavelink.Player | None = None
    guild: discord.Guild | None = None
    channel: Any = None
    display_message: Any = None
    loop_mode: str = "off"
    volume: int = 100
    suppress_end: bool = False
    started_token: str | None = None
    skip_token: str | None = None
    reconnect_required: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class YouTubePlaybackManager:
    """Lavalink player for YouTube audio and playlists."""

    def __init__(self, client: discord.Client, settings: Settings):
        self.client = client
        self.settings = settings
        self.ready = False
        self._pool_connected = False
        self._states: dict[int, LavalinkGuildState] = {}
        self.control_handler: Any = self
        # Wavelink's built-in TrackException log includes Node.__repr__ (and its URI).
        # We log the same event details below after sanitizing them.
        logging.getLogger("TrackException").setLevel(logging.CRITICAL)

    @property
    def configured(self) -> bool:
        return self.settings.lavalink_configured

    async def connect(self) -> bool:
        if not self.configured:
            log.info("YouTube audio playback is disabled: Lavalink is not configured")
            return False
        try:
            node = wavelink.Node(
                identifier="miku-lavalink",
                uri=self.settings.lavalink_connection_uri,
                password=self.settings.lavalink_password,
                client=self.client,
            )
            await wavelink.Pool.connect(nodes=[node], client=self.client)
            self._pool_connected = True
            self.ready = True
            log.info("Connected to the configured Lavalink node")
            return True
        except Exception as exc:
            self.ready = False
            log.warning(
                "Could not connect to Lavalink (%s): %s. YouTube music playback is unavailable.",
                type(exc).__name__, safe_log_detail(exc),
            )
            return False

    def state_for(self, guild_id: int) -> LavalinkGuildState:
        return self._states.setdefault(guild_id, LavalinkGuildState())

    def queue_for(self, guild_id: int) -> LavalinkGuildQueue:
        return self.state_for(guild_id).queue

    async def ensure_player(self, guild: discord.Guild, voice_channel: Any, text_channel: Any) -> wavelink.Player:
        if not self.configured or not self.ready:
            raise RuntimeError(YOUTUBE_SETUP_FEEDBACK)
        if voice_channel is None:
            raise RuntimeError("Join a voice channel first, then ask Miku to play a YouTube link or song.")
        bot_member = getattr(guild, "me", None)
        permissions_for = getattr(voice_channel, "permissions_for", None)
        if bot_member is not None and callable(permissions_for):
            permissions = permissions_for(bot_member)
            missing = [name for name in ("connect", "speak") if not getattr(permissions, name, False)]
            if missing:
                labels = " and ".join(name.title() for name in missing)
                raise RuntimeError(f"Miku needs the {labels} permission in your voice channel.")

        state = self.state_for(guild.id)
        voice = guild.voice_client
        if isinstance(voice, wavelink.Player):
            stale = voice is state.player and state.reconnect_required
            if stale or not voice.connected:
                log.info("Reconnecting stale Lavalink voice session for guild %s", guild.id)
                try:
                    await voice.disconnect(force=True)
                except Exception as exc:
                    log.warning(
                        "Could not close stale Lavalink voice session for guild %s (%s): %s",
                        guild.id, type(exc).__name__, safe_log_detail(exc),
                    )
                    raise RuntimeError(
                        "Miku couldn't reset its stale voice connection. Check Discord voice permissions and try again."
                    ) from None
                voice = None
            else:
                player = voice
                if getattr(player.channel, "id", None) != getattr(voice_channel, "id", None):
                    try:
                        await player.move_to(voice_channel)
                    except Exception as exc:
                        log.warning(
                            "Lavalink voice move failed for guild %s (%s): %s",
                            guild.id, type(exc).__name__, safe_log_detail(exc),
                        )
                        raise RuntimeError(
                            "Miku couldn't reconnect to your voice channel. Try playback again, "
                            "and check the channel's Connect and Speak permissions."
                        ) from None

        if not isinstance(voice, wavelink.Player):
            if voice is not None:
                try:
                    await voice.disconnect(force=True)
                except Exception as exc:
                    log.warning(
                        "Could not close the previous Discord voice connection for guild %s (%s): %s",
                        guild.id, type(exc).__name__, safe_log_detail(exc),
                    )
                    raise RuntimeError(
                        "Miku couldn't reset its existing voice connection. Check Discord voice permissions and try again."
                    ) from None
            try:
                player = await voice_channel.connect(cls=wavelink.Player)
            except Exception as exc:
                log.warning(
                    "Lavalink voice connection failed for guild %s (%s): %s",
                    guild.id, type(exc).__name__, safe_log_detail(exc),
                )
                raise RuntimeError(
                    "Miku couldn't connect to your voice channel. Try playback again and check its "
                    "Connect and Speak permissions."
                ) from None
        state.guild = guild
        state.player = player
        state.channel = text_channel
        state.reconnect_required = False
        return player

    @staticmethod
    def _queued(track: Track, source: PlayableSource) -> _QueuedTrack:
        if source.kind != "lavalink" or not isinstance(source.value, wavelink.Playable):
            raise RuntimeError("The YouTube result has no playable Lavalink track.")
        token = secrets.token_hex(12)
        source.value.extras = {"miku_playback_token": token}
        return _QueuedTrack(track, source.value, token)

    async def enqueue(self, guild: discord.Guild, track: Track, text_channel: Any,
                      source: PlayableSource) -> int:
        return await self.enqueue_many(guild, [(track, source)], text_channel)

    async def enqueue_many(self, guild: discord.Guild,
                           tracks: list[tuple[Track, PlayableSource]], text_channel: Any) -> int:
        state = self.state_for(guild.id)
        if state.player is None or not state.player.connected:
            raise RuntimeError(YOUTUBE_SETUP_FEEDBACK)
        queued = [self._queued(track, source) for track, source in tracks]
        if not queued:
            raise RuntimeError("The YouTube link did not return any playable tracks.")
        async with state.lock:
            state.guild = guild
            state.channel = text_channel or state.channel
            position = await state.queue.enqueue_many(queued)
            if state.queue.current_item is None and not state.player.playing:
                await self._start_next_locked(state)
            else:
                await self._update_player(state)
            return position

    async def _start_next_locked(self, state: LavalinkGuildState) -> None:
        if state.queue.upcoming and (state.player is None or not state.player.connected):
            await self._notify_failure(
                state,
                "Miku's Lavalink voice connection is unavailable. The remaining YouTube tracks are still queued; "
                "request another track after the voice connection is restored to resume them.",
            )
            return
        while state.player is not None and state.player.connected:
            item = await state.queue.start_next()
            if item is None:
                state.started_token = None
                await self._update_player(state)
                return
            state.started_token = None
            await self._update_player(state)
            try:
                await state.player.play(item.playable, volume=state.volume)
            except Exception as exc:
                log.warning(
                    "Lavalink play request failed for guild %s, requested track %s (%s): %s",
                    state.guild.id if state.guild else "unknown",
                    item.track.stable_id,
                    type(exc).__name__,
                    safe_log_detail(exc),
                )
                title = discord.utils.escape_mentions(
                    discord.utils.escape_markdown(item.track.title)
                )[:120]
                await self._notify_failure(
                    state,
                    f"Miku couldn't start **{title}** through YouTube on Lavalink. "
                    "The result may be unavailable or restricted. It is marked Failed; Miku will try the next queued track.",
                )
                await self._finish_player_message(state, "Failed")
                state.started_token = None
                await state.queue.finish_current()
                continue
            await self._update_player(state)
            return

    @staticmethod
    async def _notify_failure(state: LavalinkGuildState, message: str) -> None:
        if state.channel is None:
            return
        try:
            await state.channel.send(message)
        except (discord.HTTPException, AttributeError):
            pass

    @staticmethod
    def _payload_token(track: Any) -> str | None:
        extras = getattr(track, "extras", None)
        try:
            values = dict(extras) if extras is not None else {}
        except (TypeError, ValueError):
            values = {}
        return values.get("miku_playback_token")

    @classmethod
    def _event_token(cls, payload: Any) -> str | None:
        return (
            cls._payload_token(getattr(payload, "original", None))
            or cls._payload_token(getattr(payload, "track", None))
        )

    @classmethod
    def _event_matches_current(cls, payload: Any, current: _QueuedTrack) -> bool:
        token = cls._event_token(payload)
        # Wavelink 3.5.2 forwards userData as Playable.extras. Identifier-only
        # matching can mistake a delayed event for a repeated copy of the same video.
        return token is not None and token == current.token

    async def on_track_start(self, payload: Any) -> None:
        player = getattr(payload, "player", None)
        guild = getattr(player, "guild", None)
        state = self._states.get(guild.id) if guild else None
        if state is None:
            return
        async with state.lock:
            current = state.queue.current_item
            if current is None or not self._event_matches_current(payload, current):
                return
            state.started_token = current.token
            source = str(getattr(current.playable, "source", "YouTube"))
            log.info(
                "Lavalink started %s playback for requested track %s in guild %s",
                source, current.track.stable_id, guild.id,
            )
            await self._update_player(state)

    async def on_track_end(self, payload: Any) -> None:
        player = getattr(payload, "player", None)
        guild = getattr(player, "guild", None)
        if guild is None:
            return
        state = self._states.get(guild.id)
        if state is None:
            return
        async with state.lock:
            current = state.queue.current_item
            if current is None:
                return
            if not self._event_matches_current(payload, current):
                return
            if state.suppress_end:
                state.suppress_end = False
                state.started_token = None
                state.skip_token = None
                await self._finish_player_message(state, "Stopped")
                await state.queue.finish_current()
                return
            raw_reason = str(getattr(payload, "reason", "finished"))
            # Lavalink v4 sends camelCase values such as "loadFailed".
            # Normalize punctuation/casing rather than expecting the old enum spelling.
            reason = re.sub(r"[^A-Z0-9]", "", raw_reason.upper())
            if reason == "LOADFAILED":
                log.warning(
                    "Lavalink ended requested track %s (playback source %s) in guild %s with reason=%s",
                    current.track.stable_id,
                    getattr(current.playable, "source", "unknown"),
                    guild.id,
                    safe_log_detail(raw_reason, limit=40),
                )
                await self._notify_failure(
                    state,
                    "Lavalink couldn't load this YouTube track. It is marked Failed; "
                    "check that video is public and playable, or try another result. Miku will continue the queue.",
                )
                await self._finish_player_message(state, "Failed")
                state.started_token = None
                state.skip_token = None
                await state.queue.finish_current()
                await self._start_next_locked(state)
                return
            if reason == "STOPPED":
                skipped = state.skip_token == current.token
                state.skip_token = None
                state.started_token = None
                await self._finish_player_message(state, "Skipped" if skipped else "Stopped")
                await state.queue.finish_current()
                if skipped:
                    await self._start_next_locked(state)
                elif state.queue.upcoming:
                    await self._notify_failure(
                        state,
                        "YouTube playback stopped before the queue finished. The remaining tracks are still queued; "
                        "request another YouTube track to resume them.",
                    )
                return
            if reason != "FINISHED":
                log.warning(
                    "Lavalink ended YouTube video %s in guild %s with reason %s",
                    current.track.stable_id, guild.id, safe_log_detail(raw_reason),
                )
            completed = reason == "FINISHED" and state.started_token == current.token
            if completed:
                await self._finish_player_message(state, "Finished")
            else:
                await self._notify_failure(
                    state,
                    "Lavalink ended this YouTube track before confirming playback. It is marked Failed; "
                    "Miku will continue with the next queued track.",
                )
                await self._finish_player_message(state, "Failed")
            state.started_token = None
            state.skip_token = None
            ended = await state.queue.finish_current()
            if ended is not None and completed:
                if state.loop_mode == "track":
                    await state.queue.enqueue_front(ended)
                elif state.loop_mode == "queue":
                    await state.queue.enqueue_many([ended])
            await self._start_next_locked(state)

    async def _advance_failed_track(self, payload: Any, *, event_name: str,
                                    diagnostic: str, user_message: str) -> None:
        player = getattr(payload, "player", None)
        guild = getattr(player, "guild", None)
        state = self._states.get(guild.id) if guild else None
        if state is None:
            return
        async with state.lock:
            current = state.queue.current_item
            if current is None:
                return
            if not self._event_matches_current(payload, current):
                log.warning(
                    "Ignoring stale Lavalink %s event for guild %s: %s",
                    event_name, guild.id, diagnostic,
                )
                return
            log.warning(
                "Lavalink %s for requested track %s (playback source %s) in guild %s: %s",
                event_name, current.track.stable_id,
                getattr(current.playable, "source", "unknown"), guild.id, diagnostic,
            )
            await self._notify_failure(state, user_message)
            await self._finish_player_message(state, "Failed")
            state.started_token = None
            state.skip_token = None
            await state.queue.finish_current()
            await self._start_next_locked(state)

    async def on_track_exception(self, payload: Any) -> None:
        exception = getattr(payload, "exception", None)
        if isinstance(exception, dict):
            severity = exception.get("severity", "unknown")
            cause = exception.get("cause", "unknown")
            message = exception.get("message", "")
        else:
            severity = getattr(exception, "severity", "unknown")
            cause = getattr(exception, "cause", "unknown")
            message = getattr(exception, "message", "")
        detail = (
            f"severity={safe_log_detail(severity, limit=80)}; "
            f"cause={safe_log_detail(cause, limit=200)}; "
            f"message={safe_log_detail(message, limit=200)}"
        )
        await self._advance_failed_track(
            payload,
            event_name="TrackException",
            diagnostic=detail,
            user_message=(
                "Lavalink couldn't load this YouTube track. Miku skipped it and will continue with the queue."
            ),
        )

    async def on_track_stuck(self, payload: Any) -> None:
        threshold = getattr(payload, "threshold", "unknown")
        await self._advance_failed_track(
            payload,
            event_name="TrackStuck",
            diagnostic=f"audio output stalled beyond threshold_ms={safe_log_detail(threshold, limit=40)}",
            user_message=(
                "Audio for this YouTube track stalled. Miku skipped it and will continue with the queue."
            ),
        )

    async def on_websocket_closed(self, payload: Any) -> None:
        player = getattr(payload, "player", None)
        guild = getattr(player, "guild", None)
        state = self._states.get(guild.id) if guild is not None else None
        code = getattr(payload, "code", None)
        code_value = getattr(code, "value", code)
        by_remote = bool(getattr(payload, "by_remote", False))
        if state is not None and state.player is player and not (code_value == 4014 and by_remote):
            # Wavelink 3.5.2 only resets its connected flag for remote 4014 closes.
            # A normal close otherwise leaves a stale Player object that times out on reuse.
            state.reconnect_required = True
        log.warning(
            "Lavalink voice websocket closed for guild %s (code=%s, remote=%s): %s",
            guild.id if guild else "unknown",
            safe_log_detail(code_value if code_value is not None else "unknown", limit=40),
            safe_log_detail(by_remote, limit=20),
            safe_log_detail(getattr(payload, "reason", "")),
        )

    async def on_track_failure(self, payload: Any) -> None:
        """Compatibility entry point for older callers; events should use typed handlers."""
        await self.on_track_exception(payload)

    async def is_active_track(self, guild: discord.Guild, track: Track) -> bool:
        state = self._states.get(guild.id)
        return bool(
            state and state.queue.current_item is not None
            and state.queue.current_item.track.key == track.key
            and state.started_token == state.queue.current_item.token
            and state.player and state.player.connected and state.player.playing
        )

    def authorize_control(self, guild: discord.Guild, member: discord.Member | discord.User) -> str | None:
        voice = guild.voice_client
        if voice is None:
            return "Miku isn't connected to voice right now. Start playback with `/play` first."
        connected = getattr(voice, "connected", None)
        if connected is None:
            checker = getattr(voice, "is_connected", None)
            connected = checker() if callable(checker) else False
        if not connected:
            return "Miku isn't connected to voice right now. Start playback with `/play` first."
        member_channel = getattr(getattr(member, "voice", None), "channel", None)
        voice_channel = getattr(voice, "channel", None)
        if member_channel is None or voice_channel is None or member_channel.id != voice_channel.id:
            return "Join Miku's current voice channel to control its playback."
        return None

    async def pause(self, guild: discord.Guild) -> bool:
        state = self._states.get(guild.id)
        if state and state.player and state.player.playing:
            await state.player.pause(True)
            return True
        return False

    async def resume(self, guild: discord.Guild) -> bool:
        state = self._states.get(guild.id)
        if state and state.player and state.player.paused:
            await state.player.pause(False)
            return True
        return False

    async def skip(self, guild: discord.Guild, text_channel: Any = None) -> bool:
        state = self._states.get(guild.id)
        if not state or not state.player or state.queue.current_item is None:
            return False
        if text_channel is not None:
            state.channel = text_channel
        state.skip_token = state.queue.current_item.token
        try:
            await state.player.skip(force=True)
        except Exception:
            state.skip_token = None
            raise
        return True

    async def previous(self, guild: discord.Guild, text_channel: Any = None) -> bool:
        state = self._states.get(guild.id)
        if not state or not state.player or state.queue.current_item is None:
            return False
        if not state.queue.history:
            return False
        if text_channel is not None:
            state.channel = text_channel
        state.skip_token = state.queue.current_item.token
        await state.queue.prepare_previous()
        try:
            await state.player.skip(force=True)
        except Exception:
            state.skip_token = None
            raise
        return True

    async def stop(self, guild: discord.Guild) -> None:
        state = self._states.get(guild.id)
        if not state:
            return
        async with state.lock:
            await state.queue.clear_upcoming()
            if state.queue.current_item is not None:
                await self._finish_player_message(state, "Stopped")
            await state.queue.clear_all()
            state.started_token = None
            state.skip_token = None
            if state.player and state.player.playing:
                state.suppress_end = True
                await state.player.stop()
            state.suppress_end = False

    async def remove_queued(self, guild: discord.Guild, position: int) -> Track:
        state = self.state_for(guild.id)
        async with state.lock:
            removed = await state.queue.remove(position)
            await self._update_player(state)
            return removed

    async def clear_upcoming(self, guild: discord.Guild) -> int:
        state = self.state_for(guild.id)
        async with state.lock:
            count = await state.queue.clear_upcoming()
            await self._update_player(state)
            return count

    async def set_loop(self, guild: discord.Guild, mode: str) -> str:
        if mode not in {"off", "track", "queue"}:
            raise ValueError("Choose loop mode `off`, `track`, or `queue`.")
        state = self.state_for(guild.id)
        state.loop_mode = mode
        await self._update_player(state)
        return mode

    async def set_volume(self, guild: discord.Guild, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("Volume must be between 0 and 100.")
        state = self.state_for(guild.id)
        state.volume = value
        if state.player and state.player.connected:
            await state.player.set_volume(value)
        await self._update_player(state)
        return value

    async def shuffle(self, guild: discord.Guild) -> int:
        state = self.state_for(guild.id)
        async with state.lock:
            shuffled = await state.queue.shuffle()
            await self._update_player(state)
            return shuffled

    async def _finish_player_message(self, state: LavalinkGuildState, status: str) -> None:
        if state.display_message is None:
            return
        await self._update_player(state, status_override=status)
        state.display_message = None

    async def _update_player(
        self, state: LavalinkGuildState, *, status_override: str | None = None,
    ) -> None:
        if state.channel is None:
            return
        current, upcoming = await state.queue.snapshot()
        combined_upcoming = getattr(self.control_handler, "combined_upcoming", None)
        if callable(combined_upcoming):
            upcoming = await combined_upcoming(state.guild.id, upcoming)
        current_item = state.queue.current_item
        if current_item is None and state.display_message is None:
            return
        title_prefix = status_override or (
            "Loading" if current_item and state.started_token != current_item.token else "Now playing"
        )
        embed = track_embed(current, title_prefix=title_prefix if current else "Miku player") if current else discord.Embed(
            title="Miku player", description="Nothing is playing.", color=discord.Color.blurple()
        )
        if status_override is not None:
            status = status_override
        elif current_item is None:
            status = "Idle"
        elif state.started_token != current_item.token:
            status = "Loading"
        elif state.player and state.player.paused:
            status = "Paused"
        else:
            status = "Playing"
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Queue", value=f"{len(upcoming)} upcoming", inline=True)
        embed.add_field(name="Loop", value=state.loop_mode, inline=True)
        if upcoming:
            lines = [
                f"{index}. {discord.utils.escape_mentions(discord.utils.escape_markdown(track.title))}"
                for index, track in enumerate(upcoming[:5], 1)
            ]
            if len(upcoming) > 5:
                lines.append(f"…and {len(upcoming) - 5} more")
            embed.add_field(name="Up next", value="\n".join(lines)[:1024], inline=False)
        if current and state.player:
            position = max(0, int(state.player.position / 1000))
            embed.add_field(
                name="Progress",
                value=f"{format_duration(position)} / {format_duration(current.duration_seconds)}",
                inline=True,
            )
        try:
            if state.display_message is not None:
                await state.display_message.edit(embed=embed, view=PlayerControlsView(self.control_handler))
            else:
                state.display_message = await state.channel.send(
                    embed=embed, view=PlayerControlsView(self.control_handler)
                )
        except (discord.HTTPException, AttributeError):
            log.info("Could not update the Lavalink player message")

    async def handle_player_interaction(self, interaction: discord.Interaction, action: str) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this player in a server.", ephemeral=True)
            return
        denied = self.authorize_control(guild, interaction.user)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        actions = {"previous": self.previous, "pause": self.pause, "resume": self.resume,
                   "skip": self.skip, "stop": self.stop}
        if action == "queue":
            current, upcoming = await self.queue_for(guild.id).snapshot()
            message = self.queue_summary(current, upcoming)
        elif action == "loop":
            modes = ("off", "track", "queue")
            state = self.state_for(guild.id)
            mode = modes[(modes.index(state.loop_mode) + 1) % len(modes)]
            await self.set_loop(guild, mode)
            message = f"Loop mode: **{mode}**."
        elif operation := actions.get(action):
            result = await operation(guild, interaction.channel) if action in {"previous", "skip"} else await operation(guild)
            message = "Playback control completed." if result is not False else "There is nothing to control."
        else:
            message = "That player control is unavailable."
        await interaction.response.send_message(message, ephemeral=True)

    async def leave(self, guild: discord.Guild) -> None:
        state = self._states.get(guild.id)
        if state is None:
            return
        await self.stop(guild)
        if state.player and state.player.connected:
            await state.player.disconnect()
        self._states.pop(guild.id, None)

    async def handle_voice_disconnect(self, guild: discord.Guild) -> None:
        state = self._states.get(guild.id)
        if state is not None:
            async with state.lock:
                current = state.queue.current_item
                if current is not None:
                    await self._notify_failure(
                        state,
                        "Miku lost its Lavalink voice connection. The current YouTube track is marked Failed; "
                        "the remaining queue is preserved and will resume after reconnecting.",
                    )
                    await self._finish_player_message(state, "Failed")
                    await state.queue.finish_current()
                state.started_token = None
                state.skip_token = None
                state.player = None
                state.reconnect_required = True

    async def close(self) -> None:
        for state in list(self._states.values()):
            if state.player and state.player.connected:
                try:
                    await state.player.disconnect()
                except discord.HTTPException:
                    pass
        self._states.clear()
        if self._pool_connected:
            try:
                await wavelink.Pool.close()
            except Exception as exc:
                log.info("Could not close Lavalink pool (%s)", type(exc).__name__)
            self._pool_connected = False
        self.ready = False

    @staticmethod
    def queue_summary(current: Track | None, upcoming: tuple[Track, ...]) -> str:
        lines = [f"**Now playing:** {current.title}" ] if current else []
        lines.extend(f"{index}. {track.title}" for index, track in enumerate(upcoming, 1))
        return "\n".join(lines)[:1900] if lines else "The queue is empty."


class HybridPlaybackManager:
    """Keep the existing control interface while routing music only to YouTube/Lavalink."""

    def __init__(self, native: PlaybackManager, youtube: YouTubePlaybackManager,
                 youtube_provider: YouTubeSearchProvider):
        self.native = native
        self.youtube = youtube
        self.youtube_provider = youtube_provider
        self.active: dict[int, str] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._pending: dict[int, deque[_DeferredTrack]] = {}
        self._switching: set[int] = set()
        self.youtube.control_handler = self

    def player_view(self) -> PlayerControlsView:
        return PlayerControlsView(self)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def backend_for(self, guild_id: int) -> str:
        return "youtube"

    def queue_for(self, guild_id: int):
        return self.youtube.queue_for(guild_id)

    @staticmethod
    def _voice_busy(voice: Any) -> bool:
        if voice is None:
            return False
        playing = getattr(voice, "playing", None)
        paused = getattr(voice, "paused", None)
        if playing is None:
            checker = getattr(voice, "is_playing", None)
            playing = checker() if callable(checker) else False
        if paused is None:
            checker = getattr(voice, "is_paused", None)
            paused = checker() if callable(checker) else False
        return bool(playing or paused)

    async def queue_snapshot(self, guild_id: int) -> tuple[Track | None, tuple[Track, ...]]:
        current, upcoming = await self.queue_for(guild_id).snapshot()
        pending = self._pending.get(guild_id, ())
        return current, (*upcoming, *(item.track for item in pending))

    async def has_active_playback(self, guild: discord.Guild) -> bool:
        guild_id = guild.id
        if self._pending.get(guild_id):
            return True
        current, upcoming = await self.queue_for(guild_id).snapshot()
        if current is not None or upcoming:
            return True
        state = self.youtube.state_for(guild_id)
        player = state.player
        return bool(player and (player.playing or player.paused))

    async def combined_upcoming(self, guild_id: int, upcoming: tuple[Track, ...]) -> tuple[Track, ...]:
        return (*upcoming, *(item.track for item in self._pending.get(guild_id, ())))

    @staticmethod
    def queue_summary(current: Track | None, upcoming: tuple[Track, ...]) -> str:
        return PlaybackManager.queue_summary(current, upcoming)

    def authorize_control(self, guild: discord.Guild, member: discord.Member | discord.User) -> str | None:
        return self.youtube.authorize_control(guild, member)

    async def _prepare_youtube_locked(
        self, guild: discord.Guild, voice_channel: Any, text_channel: Any,
    ) -> None:
        state = self.youtube.state_for(guild.id)
        player = state.player
        current = state.queue.current_item
        disconnected = state.reconnect_required or player is None or not player.connected

        active_player = bool(player and (player.playing or player.paused))
        if not disconnected and (current is not None or active_player):
            if getattr(player.channel, "id", None) != getattr(voice_channel, "id", None):
                raise RuntimeError(
                    "Miku is already playing in a different voice channel. Join that channel or wait for its queue to end."
                )
            state.guild = guild
            state.channel = text_channel or state.channel
            return

        if disconnected and current is not None:
            async with state.lock:
                current = state.queue.current_item
                if current is not None:
                    log.warning(
                        "Lavalink voice session ended while track %s was current in guild %s; preserving upcoming queue",
                        current.track.stable_id, guild.id,
                    )
                    await self.youtube._notify_failure(
                        state,
                        "Miku lost its Lavalink voice connection before this YouTube track finished. "
                        "It is marked Failed; the remaining queue will be kept and resumed after reconnecting.",
                    )
                    await self.youtube._finish_player_message(state, "Failed")
                    state.started_token = None
                    state.skip_token = None
                    await state.queue.finish_current()

        await self.youtube.ensure_player(guild, voice_channel, text_channel)
        if state.queue.current_item is None and state.queue.upcoming:
            async with state.lock:
                await self.youtube._start_next_locked(state)

    async def prepare_youtube(self, guild: discord.Guild, voice_channel: Any, text_channel: Any) -> None:
        async with self._lock(guild.id):
            await self._prepare_youtube_locked(guild, voice_channel, text_channel)

    async def _backend_busy(self, guild: discord.Guild, backend: str) -> bool:
        manager = self.youtube if backend == "youtube" else self.native
        current, upcoming = await manager.queue_for(guild.id).snapshot()
        if current is not None or upcoming:
            return True
        if backend == "youtube":
            player = self.youtube.state_for(guild.id).player
            return bool(player and (player.playing or player.paused))
        voice = guild.voice_client
        return self._voice_busy(voice)

    async def _refresh_active_player(self, guild_id: int) -> None:
        if self.backend_for(guild_id) == "youtube":
            await self.youtube._update_player(self.youtube.state_for(guild_id))
        else:
            await self.native._update_player(guild_id)

    async def _activate_deferred(self, guild: discord.Guild, item: _DeferredTrack) -> None:
        if item.track.provider != "youtube" or item.source is None:
            raise RuntimeError("Miku currently plays YouTube audio only.")
        await self._prepare_youtube_locked(guild, item.voice_channel, item.text_channel)
        await self.youtube.enqueue(guild, item.track, item.text_channel, item.source)

    def ignores_voice_disconnect(self, guild: discord.Guild) -> bool:
        state = self.youtube.state_for(guild.id)
        return bool(state.player and state.player.connected and not state.reconnect_required)

    async def _drain_deferred_locked(self, guild: discord.Guild) -> None:
        pending = self._pending.get(guild.id)
        if not pending or await self._backend_busy(guild, self.backend_for(guild.id)):
            return
        while pending:
            item = pending[0]
            try:
                await self._activate_deferred(guild, item)
            except Exception as exc:
                pending.popleft()
                log.warning(
                    "Could not start deferred %s track %s in guild %s (%s)",
                    item.track.provider, item.track.stable_id, guild.id, type(exc).__name__,
                )
                try:
                    await item.text_channel.send(
                        f"I couldn't start **{discord.utils.escape_markdown(item.track.title)}** "
                        "when the previous queue ended. Try requesting it again."
                    )
                except (discord.HTTPException, AttributeError):
                    pass
                if await self._backend_busy(guild, self.backend_for(guild.id)):
                    break
                continue
            pending.popleft()
            if await self._backend_busy(guild, self.backend_for(guild.id)):
                # Consecutive requests handled by the now active backend keep normal FIFO order.
                continue
        if not pending:
            self._pending.pop(guild.id, None)
        await self._refresh_active_player(guild.id)

    async def on_native_queue_idle(self, guild: discord.Guild) -> None:
        async with self._lock(guild.id):
            if self.backend_for(guild.id) == "native":
                await self._drain_deferred_locked(guild)

    async def on_youtube_track_end(self, payload: Any) -> None:
        await self.youtube.on_track_end(payload)
        await self._drain_after_youtube_event(payload)

    async def on_youtube_track_exception(self, payload: Any) -> None:
        await self.youtube.on_track_exception(payload)
        await self._drain_after_youtube_event(payload)

    async def on_youtube_track_stuck(self, payload: Any) -> None:
        await self.youtube.on_track_stuck(payload)
        await self._drain_after_youtube_event(payload)

    async def _drain_after_youtube_event(self, payload: Any) -> None:
        player = getattr(payload, "player", None)
        guild = getattr(player, "guild", None)
        if guild is not None:
            async with self._lock(guild.id):
                if self.backend_for(guild.id) == "youtube":
                    await self._drain_deferred_locked(guild)

    async def enqueue(self, guild: discord.Guild, track: Track, text_channel: Any,
                      *, resolved_source: PlayableSource | None = None,
                      voice_channel: Any = None) -> int:
        if track.provider != "youtube":
            raise RuntimeError("Miku currently plays YouTube audio only.")
        source = resolved_source or await self.youtube_provider.resolve(track)
        if source is None:
            raise RuntimeError(
                "This YouTube video is unavailable or unsupported by the configured audio server."
            )
        async with self._lock(guild.id):
            await self._prepare_youtube_locked(guild, voice_channel, text_channel)
            return await self.youtube.enqueue(guild, track, text_channel, source)

    async def enqueue_youtube_playlist(self, guild: discord.Guild,
                                       tracks: list[tuple[Track, PlayableSource]],
                                       text_channel: Any, *, voice_channel: Any = None) -> int:
        if not tracks:
            return 0
        if any(track.provider != "youtube" for track, _ in tracks):
            raise RuntimeError("Miku currently plays YouTube audio only.")
        async with self._lock(guild.id):
            await self._prepare_youtube_locked(guild, voice_channel, text_channel)
            return await self.youtube.enqueue_many(guild, tracks, text_channel)

    async def is_active_track(self, guild: discord.Guild, track: Track) -> bool:
        manager = self.youtube if self.backend_for(guild.id) == "youtube" else self.native
        return await manager.is_active_track(guild, track)

    def _manager(self, guild: discord.Guild):
        return self.youtube if self.backend_for(guild.id) == "youtube" else self.native

    async def pause(self, guild: discord.Guild) -> bool:
        return await self._manager(guild).pause(guild)

    async def resume(self, guild: discord.Guild) -> bool:
        return await self._manager(guild).resume(guild)

    async def skip(self, guild: discord.Guild, text_channel: Any = None) -> bool:
        result = await self._manager(guild).skip(guild, text_channel)
        if result and self.backend_for(guild.id) == "native":
            async with self._lock(guild.id):
                await self._drain_deferred_locked(guild)
        return result

    async def previous(self, guild: discord.Guild, text_channel: Any = None) -> bool:
        return await self._manager(guild).previous(guild, text_channel)

    async def stop(self, guild: discord.Guild) -> None:
        async with self._lock(guild.id):
            self._pending.pop(guild.id, None)
            await self._manager(guild).stop(guild)

    async def remove_queued(self, guild: discord.Guild, position: int) -> Track:
        async with self._lock(guild.id):
            current, upcoming = await self.queue_for(guild.id).snapshot()
            if position <= len(upcoming):
                removed = await self._manager(guild).remove_queued(guild, position)
            else:
                pending = self._pending.get(guild.id, deque())
                offset = position - len(upcoming) - 1
                if position < 1 or offset >= len(pending):
                    raise IndexError("queue position is out of range")
                items = list(pending)
                removed = items.pop(offset).track
                if items:
                    self._pending[guild.id] = deque(items)
                else:
                    self._pending.pop(guild.id, None)
            await self._refresh_active_player(guild.id)
            return removed

    async def clear_upcoming(self, guild: discord.Guild) -> int:
        async with self._lock(guild.id):
            cleared = await self._manager(guild).clear_upcoming(guild)
            pending = self._pending.pop(guild.id, deque())
            await self._refresh_active_player(guild.id)
            return cleared + len(pending)

    async def set_loop(self, guild: discord.Guild, mode: str) -> str:
        return await self._manager(guild).set_loop(guild, mode)

    async def set_volume(self, guild: discord.Guild, value: int) -> int:
        return await self._manager(guild).set_volume(guild, value)

    async def shuffle(self, guild: discord.Guild) -> int:
        async with self._lock(guild.id):
            shuffled = await self._manager(guild).shuffle(guild)
            pending = self._pending.get(guild.id)
            if pending:
                items = list(pending)
                random.shuffle(items)
                self._pending[guild.id] = deque(items)
            await self._refresh_active_player(guild.id)
            return shuffled + len(pending or ())

    async def leave(self, guild: discord.Guild) -> None:
        self._pending.pop(guild.id, None)
        if self.backend_for(guild.id) == "youtube":
            await self.youtube.leave(guild)
            self.native.queues.discard_guild(guild.id)
            display = self.native._displays.pop(guild.id, None)
            if display:
                self.native._cancel_refresh(guild.id)
        else:
            await self.native.leave(guild)
            await self.youtube.leave(guild)
        self.active.pop(guild.id, None)

    async def handle_voice_disconnect(self, guild: discord.Guild) -> None:
        if self.ignores_voice_disconnect(guild):
            return
        self._pending.pop(guild.id, None)
        self.active.pop(guild.id, None)
        await self.youtube.handle_voice_disconnect(guild)

    async def handle_player_interaction(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None:
            await self.native.handle_player_interaction(interaction, action)
            return
        if action == "queue":
            current, upcoming = await self.queue_snapshot(interaction.guild.id)
            await interaction.response.send_message(self.queue_summary(current, upcoming), ephemeral=True)
            return
        await self._manager(interaction.guild).handle_player_interaction(interaction, action)

    async def close(self) -> None:
        await self.youtube.close()
