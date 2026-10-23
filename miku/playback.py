from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import discord

from .models import PlayableSource, Track
from .providers import ProviderRegistry
from .queue import GuildQueue, QueueManager

log = logging.getLogger(__name__)

PLAYER_REFRESH_SECONDS = 20


@dataclass
class PlaybackDisplay:
    channel: Any
    message: Any = None
    view: discord.ui.View | None = None
    status: str = "Loading"
    last_track: Track | None = None
    started_at: float | None = None
    paused_at: float | None = None
    paused_total: float = 0.0
    refresh_task: asyncio.Task | None = None
    generation: int = 0


class PlayerControlsView(discord.ui.View):
    """Persistent player controls; after a restart callbacks explain that the queue ended."""

    def __init__(self, manager: "PlaybackManager"):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary,
                       custom_id="miku:previous", row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "previous")

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary,
                       custom_id="miku:pause", row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "pause")

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.primary,
                       custom_id="miku:resume", row=0)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "resume")

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary,
                       custom_id="miku:skip", row=1)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "skip")

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary,
                       custom_id="miku:queue", row=1)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "queue")

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary,
                       custom_id="miku:loop", row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "loop")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger,
                       custom_id="miku:stop", row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.manager.handle_player_interaction(interaction, "stop")


class PlaybackManager:
    """Per-guild voice sessions backed by local files or explicitly authorized streams."""

    def __init__(self, providers: ProviderRegistry, idle_disconnect_seconds: int = 0):
        self.providers = providers
        self.idle_disconnect_seconds = idle_disconnect_seconds
        self.queues = QueueManager()
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._idle_tasks: dict[int, asyncio.Task[None]] = {}
        self._displays: dict[int, PlaybackDisplay] = {}
        self.loop_modes: dict[int, str] = defaultdict(lambda: "off")
        self.volumes: dict[int, int] = defaultdict(lambda: 100)
        self.control_handler: Any = self

    def queue_for(self, guild_id: int) -> GuildQueue:
        return self.queues.for_guild(guild_id)

    async def is_active_track(self, guild: discord.Guild, track: Track) -> bool:
        current, _ = await self.queue_for(guild.id).snapshot()
        voice = guild.voice_client
        return bool(current and current.key == track.key and voice and voice.is_playing())

    def player_view(self) -> PlayerControlsView:
        return PlayerControlsView(self.control_handler)

    def authorize_control(self, guild: discord.Guild, member: discord.Member | discord.User) -> str | None:
        voice = guild.voice_client
        if voice is None or not voice.is_connected():
            return "Miku isn't connected to voice right now. Start playback with `/play` first."
        member_channel = getattr(getattr(member, "voice", None), "channel", None)
        voice_channel = getattr(voice, "channel", None)
        if member_channel is None or voice_channel is None or member_channel.id != voice_channel.id:
            return "Join Miku's current voice channel to control its playback."
        return None

    def _display(self, guild_id: int, channel: Any) -> PlaybackDisplay:
        display = self._displays.get(guild_id)
        if display is None:
            display = PlaybackDisplay(channel=channel, view=self.player_view())
            self._displays[guild_id] = display
        elif channel is not None:
            display.channel = channel
        return display

    def _cancel_idle(self, guild_id: int) -> None:
        task = self._idle_tasks.pop(guild_id, None)
        if task:
            task.cancel()

    def _position(self, display: PlaybackDisplay) -> float:
        if display.started_at is None:
            return 0.0
        now = display.paused_at if display.paused_at is not None else time.monotonic()
        return max(0.0, now - display.started_at - display.paused_total)

    def _player_embed(self, guild_id: int) -> discord.Embed:
        display = self._displays[guild_id]
        track = self.queue_for(guild_id).current or display.last_track
        if track is None:
            embed = discord.Embed(title="Miku music player", color=discord.Color.blurple())
            embed.description = "Nothing is playing. Use `/play <song>` to start a track."
            embed.add_field(name="Status", value=display.status, inline=True)
            return embed

        duration = format_duration(track.duration_seconds)
        position = format_duration(int(self._position(display)))
        embed = discord.Embed(
            title=f"{display.status}: {_safe_title(track.title)}"[:256],
            url=(track.source_url if track.source_url and track.source_url.startswith("https://") else None),
            description=(f"**Artist:** {_safe_title(track.artist or 'Unknown artist')}\n"
                         f"**Provider:** {track.provider} · **Position:** {position} / {duration}"),
            color=discord.Color.green() if display.status == "Playing" else discord.Color.blurple(),
        )
        if track.requester:
            embed.add_field(name="Requested by", value=_safe_title(track.requester)[:1024], inline=True)
        if track.album:
            release = f" ({track.release_date})" if track.release_date else ""
            embed.add_field(name="Album / release", value=f"{_safe_title(track.album)}{release}"[:1024], inline=False)
        if track.thumbnail_url and track.thumbnail_url.startswith("https://"):
            embed.set_thumbnail(url=track.thumbnail_url)
        if track.source_url and track.source_url.startswith("https://"):
            embed.add_field(name="Source", value=f"[Open track page]({track.source_url})", inline=True)
        if track.license_url:
            embed.add_field(
                name="Credit / license",
                value=f"{_safe_title(track.artist or 'Unknown artist')} · Jamendo · {_safe_title(track.license_url)}",
                inline=False,
            )
        embed.set_footer(text="Player controls · Previous replays the last track")
        return embed

    async def _update_player(self, guild_id: int) -> None:
        display = self._displays.get(guild_id)
        if display is None or display.channel is None:
            return
        queue = self.queue_for(guild_id)
        current, upcoming = await queue.snapshot()
        combined_upcoming = getattr(self.control_handler, "combined_upcoming", None)
        if callable(combined_upcoming):
            upcoming = await combined_upcoming(guild_id, upcoming)
        shown_track = current or display.last_track
        embed = self._player_embed(guild_id)
        embed.set_footer(text=f"{len(upcoming)} upcoming track(s) · Previous replays the last track")
        embed.add_field(name="Loop", value=self.loop_modes[guild_id], inline=True)
        embed.add_field(name="Volume", value=f"{self.volumes[guild_id]}%", inline=True)
        if upcoming:
            lines = [f"{index}. {_safe_title(track.title)}" for index, track in enumerate(upcoming[:5], 1)]
            if len(upcoming) > 5:
                lines.append(f"…and {len(upcoming) - 5} more")
            embed.add_field(name="Up next", value="\n".join(lines)[:1024], inline=False)
        if shown_track and shown_track.duration_seconds is not None and display.status in {"Playing", "Paused"}:
            embed.description = (embed.description or "") + (
                f"\n**Progress:** {format_duration(int(self._position(display)))} / "
                f"{format_duration(shown_track.duration_seconds)}"
            )
        try:
            if display.message is not None:
                await display.message.edit(embed=embed, view=display.view)
            else:
                display.message = await display.channel.send(embed=embed, view=display.view)
        except discord.NotFound:
            display.message = None
            try:
                display.message = await display.channel.send(embed=embed, view=display.view)
            except (discord.HTTPException, AttributeError):
                log.info("Could not recreate a deleted player message in guild %s", guild_id)
        except (discord.HTTPException, AttributeError):
            log.info("Could not update the player message in guild %s", guild_id)

    def _schedule_refresh(self, guild_id: int) -> None:
        display = self._displays[guild_id]
        if display.refresh_task and not display.refresh_task.done():
            return
        display.refresh_task = asyncio.create_task(self._refresh_loop(guild_id))

    async def _refresh_loop(self, guild_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(PLAYER_REFRESH_SECONDS)
                display = self._displays.get(guild_id)
                if display is None or display.status not in {"Playing", "Paused"}:
                    return
                await self._update_player(guild_id)
        except asyncio.CancelledError:
            return

    def _cancel_refresh(self, guild_id: int) -> None:
        display = self._displays.get(guild_id)
        task = display.refresh_task if display else None
        if task and task is not asyncio.current_task():
            task.cancel()
            display.refresh_task = None

    async def enqueue(self, guild: discord.Guild, track: Track, text_channel: Any,
                      *, resolved_source: PlayableSource | None = None) -> int:
        if not self.providers.supports_playback(track) and resolved_source is None:
            raise RuntimeError("This result has no enabled, authorized audio source.")
        voice = guild.voice_client
        if voice is None or not voice.is_connected():
            raise RuntimeError("Join a voice channel first; then use `/play`.")
        self._cancel_idle(guild.id)
        async with self._locks[guild.id]:
            display = self._display(guild.id, text_channel)
            display.status = "Loading" if not (voice.is_playing() or voice.is_paused()) else display.status
            was_idle = not voice.is_playing() and not voice.is_paused()
            position = await self.queue_for(guild.id).enqueue(track)
            if not voice.is_playing() and not voice.is_paused():
                await self._start_next_locked(
                    guild, display.channel, first_source=resolved_source,
                    quiet_track_key=track.key,
                )
            else:
                await self._update_player(guild.id)
            if was_idle and position == 1:
                current, _ = await self.queue_for(guild.id).snapshot()
                if current is None or current.key != track.key or not voice.is_playing():
                    raise RuntimeError(
                        "I couldn't start that track because its audio source is unavailable. Choose another result."
                    )
            return position

    async def start_next(self, guild: discord.Guild, text_channel: Any = None) -> None:
        async with self._locks[guild.id]:
            await self._start_next_locked(guild, text_channel)

    async def _start_next_locked(self, guild: discord.Guild, text_channel: Any = None,
                                 *, first_source: PlayableSource | None = None,
                                 quiet_track_key: tuple[str, str] | None = None) -> None:
        voice = guild.voice_client
        if voice is None or not voice.is_connected() or voice.is_playing() or voice.is_paused():
            return
        display = self._display(guild.id, text_channel)
        self._cancel_idle(guild.id)
        queue = self.queue_for(guild.id)
        source_override = first_source
        while True:
            track = await queue.start_next()
            if track is None:
                self._cancel_refresh(guild.id)
                if display.status not in {"Stopped", "Skipped", "Failed"}:
                    display.status = "Finished" if display.last_track is not None else "Idle"
                display.started_at = None
                display.paused_at = None
                await self._update_player(guild.id)
                receiving = bool(getattr(voice, "is_listening", lambda: False)())
                if self.idle_disconnect_seconds and not receiving:
                    task = asyncio.create_task(self._idle_disconnect(guild, self.idle_disconnect_seconds))
                    self._idle_tasks[guild.id] = task
                return
            display.last_track = track
            display.status = "Loading"
            await self._update_player(guild.id)
            try:
                source = source_override or await self.providers.resolve(track)
                source_override = None
                if source is None or source.kind not in {"file", "http"}:
                    raise RuntimeError("No authorized playable source is available.")
                before_options = "-nostdin"
                if source.kind == "http":
                    before_options += " -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                audio = discord.FFmpegPCMAudio(
                    source.value,
                    before_options=before_options,
                    options="-vn",
                    stderr=subprocess.DEVNULL,
                )
                loop = asyncio.get_running_loop()

                display.generation += 1
                generation = display.generation

                def after(error: Exception | None) -> None:
                    future = asyncio.run_coroutine_threadsafe(
                        self._after_audio(guild, error, generation, voice_client=voice), loop
                    )
                    # Retrieving the result prevents unobserved callback failures from being silent.
                    future.add_done_callback(self._log_callback_failure)

                audio = discord.PCMVolumeTransformer(audio, volume=self.volumes[guild.id] / 100)
                voice.play(audio, after=after)
                display.status = "Playing"
                display.last_track = track
                display.started_at = time.monotonic()
                display.paused_at = None
                display.paused_total = 0.0
                self._schedule_refresh(guild.id)
                await self._update_player(guild.id)
                return
            except Exception as exc:
                log.info(
                    "Could not play provider=%s track_id=%s (%s)",
                    track.provider,
                    track.stable_id,
                    type(exc).__name__,
                )
                display.status = "Loading"
                if track.key != quiet_track_key:
                    try:
                        await display.channel.send(
                            f"I couldn't play **{_safe_title(track.title)}**. "
                            "Its audio source is unavailable or FFmpeg could not open it."
                        )
                    except (discord.HTTPException, AttributeError):
                        pass

    async def _after_audio(self, guild: discord.Guild, error: Exception | None,
                           generation: int | None = None, *, voice_client: Any = None) -> None:
        if error:
            log.warning("Audio playback ended with an error in guild %s (%s)", guild.id, type(error).__name__)
            display = self._displays.get(guild.id)
            if display and display.channel:
                try:
                    await display.channel.send(
                        "⚠️ Audio playback stopped unexpectedly. Miku will try the next queued track."
                    )
                except (discord.HTTPException, AttributeError):
                    pass
        async with self._locks[guild.id]:
            if voice_client is not None and guild.voice_client is not voice_client:
                return
            display = self._displays.get(guild.id)
            if display is not None and generation is not None and display.generation != generation:
                return
            queue = self.queue_for(guild.id)
            current = queue.current
            if current is not None and error is None:
                mode = self.loop_modes[guild.id]
                if mode == "track":
                    await queue.enqueue_front(current)
                elif mode == "queue":
                    await queue.enqueue(current)
            await self._start_next_locked(guild)
        current, upcoming = await self.queue_for(guild.id).snapshot()
        if current is None and not upcoming:
            callback = getattr(self.control_handler, "on_native_queue_idle", None)
            if callable(callback):
                await callback(guild)

    @staticmethod
    def _log_callback_failure(future) -> None:
        try:
            error = future.exception()
        except (asyncio.CancelledError, Exception) as exc:
            log.warning("Could not advance the guild queue (%s)", type(exc).__name__)
            return
        if error is not None:
            log.warning("Could not advance the guild queue (%s)", type(error).__name__)

    async def _idle_disconnect(self, guild: discord.Guild, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            voice = guild.voice_client
            queue = self.queue_for(guild.id)
            _, upcoming = await queue.snapshot()
            receiving = bool(voice and getattr(voice, "is_listening", lambda: False)())
            if voice and voice.is_connected() and not voice.is_playing() and not upcoming and not receiving:
                await voice.disconnect()
        except asyncio.CancelledError:
            return
        except discord.HTTPException:
            log.info("Idle voice disconnect failed in guild %s", guild.id)
        finally:
            if self._idle_tasks.get(guild.id) is asyncio.current_task():
                self._idle_tasks.pop(guild.id, None)

    async def pause(self, guild: discord.Guild) -> bool:
        async with self._locks[guild.id]:
            voice = guild.voice_client
            if voice and voice.is_playing():
                voice.pause()
                display = self._displays.get(guild.id)
                if display:
                    display.paused_at = time.monotonic()
                    display.status = "Paused"
                    await self._update_player(guild.id)
                return True
            return False

    async def resume(self, guild: discord.Guild) -> bool:
        async with self._locks[guild.id]:
            voice = guild.voice_client
            if voice and voice.is_paused():
                voice.resume()
                display = self._displays.get(guild.id)
                if display:
                    now = time.monotonic()
                    if display.paused_at is not None:
                        display.paused_total += now - display.paused_at
                    display.paused_at = None
                    display.status = "Playing"
                    await self._update_player(guild.id)
                return True
            return False

    async def remove_queued(self, guild: discord.Guild, position: int) -> Track:
        async with self._locks[guild.id]:
            track = await self.queue_for(guild.id).remove(position)
            await self._update_player(guild.id)
            return track

    async def clear_upcoming(self, guild: discord.Guild) -> int:
        async with self._locks[guild.id]:
            count = await self.queue_for(guild.id).clear_upcoming()
            await self._update_player(guild.id)
            return count

    async def set_loop(self, guild: discord.Guild, mode: str) -> str:
        if mode not in {"off", "track", "queue"}:
            raise ValueError("Choose loop mode `off`, `track`, or `queue`.")
        self.loop_modes[guild.id] = mode
        await self._update_player(guild.id)
        return mode

    async def set_volume(self, guild: discord.Guild, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("Volume must be between 0 and 100.")
        self.volumes[guild.id] = value
        voice = guild.voice_client
        source = getattr(voice, "source", None) if voice else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = value / 100
        await self._update_player(guild.id)
        return value

    async def shuffle(self, guild: discord.Guild) -> int:
        async with self._locks[guild.id]:
            count = await self.queue_for(guild.id).shuffle_upcoming()
            await self._update_player(guild.id)
            return count

    async def skip(self, guild: discord.Guild, text_channel: Any = None) -> bool:
        async with self._locks[guild.id]:
            voice = guild.voice_client
            queue = self.queue_for(guild.id)
            current, upcoming = await queue.snapshot()
            if current is None and not upcoming:
                return False
            display = self._display(guild.id, text_channel)
            if current is not None:
                display.generation += 1
                await queue.finish_current()
            if voice and (voice.is_playing() or voice.is_paused()):
                display.status = "Loading"
                voice.stop()
            await self._start_next_locked(guild, display.channel)
            return True

    async def previous(self, guild: discord.Guild, text_channel: Any = None) -> bool:
        async with self._locks[guild.id]:
            voice = guild.voice_client
            queue = self.queue_for(guild.id)
            changed = await queue.prepare_previous()
            if not changed:
                return False
            display = self._display(guild.id, text_channel)
            display.status = "Loading"
            display.generation += 1
            if voice and (voice.is_playing() or voice.is_paused()):
                voice.stop()
            await self._start_next_locked(guild, display.channel)
            return True

    async def stop(self, guild: discord.Guild) -> None:
        self._cancel_idle(guild.id)
        async with self._locks[guild.id]:
            voice = guild.voice_client
            queue = self.queue_for(guild.id)
            current, _ = await queue.snapshot()
            display = self._displays.get(guild.id)
            active = bool(
                current is not None
                or voice and (voice.is_playing() or voice.is_paused())
            )
            if active and display:
                display.generation += 1
            await queue.clear_all()
            if voice and (voice.is_playing() or voice.is_paused()):
                voice.stop()
            if display:
                self._cancel_refresh(guild.id)
                if active:
                    display.status = "Stopped"
                display.started_at = None
                display.paused_at = None
                await self._update_player(guild.id)

    async def leave(self, guild: discord.Guild) -> None:
        await self.stop(guild)
        voice = guild.voice_client
        if voice and voice.is_connected():
            if hasattr(voice, "stop_listening"):
                voice.stop_listening()
            await voice.disconnect()
        self.queues.discard_guild(guild.id)
        display = self._displays.get(guild.id)
        if display:
            self._cancel_refresh(guild.id)
        self._displays.pop(guild.id, None)

    async def handle_voice_disconnect(self, guild: discord.Guild) -> None:
        """Drop runtime queue state after Discord disconnects the bot unexpectedly."""
        async with self._locks[guild.id]:
            await self.queue_for(guild.id).clear_all()
            display = self._displays.get(guild.id)
            if display:
                self._cancel_refresh(guild.id)
                display.generation += 1
                display.status = "Stopped"
                display.started_at = None
                display.paused_at = None
                await self._update_player(guild.id)

    async def handle_player_interaction(self, interaction: discord.Interaction, action: str) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this player in a server.", ephemeral=True)
            return
        display = self._displays.get(guild.id)
        if display is None:
            await interaction.response.send_message(
                "This player session ended when Miku restarted. Queues don't resume across restarts; "
                "start a new track with `/play`.", ephemeral=True,
            )
            return
        denied = self.authorize_control(guild, interaction.user)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        channel = display.channel or interaction.channel
        if action == "queue":
            current, upcoming = await self.queue_for(guild.id).snapshot()
            summary = self.queue_summary(current, upcoming)
            await interaction.response.send_message(summary, ephemeral=True)
            return
        if action == "loop":
            modes = ("off", "track", "queue")
            current_mode = self.loop_modes[guild.id]
            mode = modes[(modes.index(current_mode) + 1) % len(modes)]
            await self.set_loop(guild, mode)
            await interaction.response.send_message(f"Loop mode: **{mode}**.", ephemeral=True)
            return
        actions = {
            "previous": self.previous,
            "pause": self.pause,
            "resume": self.resume,
            "skip": self.skip,
            "stop": self.stop,
        }
        operation = actions.get(action)
        if operation is None:
            await interaction.response.send_message("That player control is unavailable.", ephemeral=True)
            return
        if action in {"previous", "skip"}:
            changed = await operation(guild, channel)
        else:
            result = await operation(guild)
            changed = bool(result) if action != "stop" else True
        if action == "stop":
            message = "Stopped playback and cleared the queue. Miku remains connected; use `/leave` to disconnect."
        elif changed:
            message = {
                "previous": "Replaying the previous track.",
                "pause": "Paused playback.",
                "resume": "Resumed playback.",
                "skip": "Skipped to the next track.",
            }[action]
        else:
            message = {
                "previous": "There isn't a previous track in this session.",
                "pause": "Nothing is playing.",
                "resume": "Playback isn't paused.",
                "skip": "The queue is empty.",
            }[action]
        await interaction.response.send_message(message, ephemeral=True)

    @staticmethod
    def queue_summary(current: Track | None, upcoming: tuple[Track, ...]) -> str:
        lines = [f"**Now playing:** {_safe_title(current.title)}"] if current else []
        lines.extend(f"{index}. {_safe_title(track.title)}" for index, track in enumerate(upcoming, 1))
        return "\n".join(lines)[:1900] if lines else "The queue is empty."


def _safe_title(value: str) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(value.replace("\n", " ").strip())
    )


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Duration unavailable"
    minutes, remaining = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{remaining:02}" if hours else f"{minutes}:{remaining:02}"


def track_embed(track: Track, *, title_prefix: str = "Search result") -> discord.Embed:
    embed = discord.Embed(
        title=f"{title_prefix}: {_safe_title(track.title)[:240]}",
        url=track.source_url if track.source_url and track.source_url.startswith("https://") else None,
        description=(f"**Artist / channel:** {_safe_title(track.artist or 'Unknown')}\n"
                     f"**Album:** {_safe_title(track.album or 'Unknown')}\n"
                     f"**Duration:** {format_duration(track.duration_seconds)}\n"
                     f"**Provider:** {track.provider}"),
        color=discord.Color.blurple(),
    )
    if track.requester:
        embed.add_field(name="Requested by", value=_safe_title(track.requester)[:1024], inline=True)
    if track.thumbnail_url and track.thumbnail_url.startswith("https://"):
        embed.set_thumbnail(url=track.thumbnail_url)
    if track.source_url and track.source_url.startswith("https://"):
        embed.add_field(name="Source", value=f"[Open source]({track.source_url})", inline=False)
    if track.license_url:
        embed.add_field(
            name="License / credit",
            value=f"{_safe_title(track.artist or 'Unknown artist')} · Jamendo · {_safe_title(track.license_url)}",
            inline=False,
        )
    if track.official_source is True:
        embed.add_field(name="Source verification", value="Official source verified by provider metadata", inline=False)
    else:
        embed.add_field(name="Source verification", value="Not verified by provider metadata", inline=False)
    return embed
