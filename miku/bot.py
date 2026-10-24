from __future__ import annotations

import logging
import traceback
from dataclasses import replace
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings
from .database import Database
from .models import SearchOutcome, Track
from .playback import PlaybackManager, PlayerControlsView, format_duration, track_embed
from .lavalink_playback import (
    HybridPlaybackManager,
    YOUTUBE_SETUP_FEEDBACK,
    YouTubePlaybackManager,
    safe_log_detail,
)
from .providers import ProviderError, ProviderRegistry, YouTubeSearchProvider
from .service import SongMemoryService
from .voice import VoiceCommandParser
from .voice_control import VoiceController

log = logging.getLogger(__name__)

YOUTUBE_CONFIG_FEEDBACK = (
    "⚙️ YouTube search isn't configured yet. The bot owner needs to set "
    "`YOUTUBE_API_KEY` as an environment variable or in the local `.env` file."
)
NO_RESULTS_FEEDBACK = "🔎 No matching songs were found. Try another song title or artist."
UNSUPPORTED_URL_FEEDBACK = (
    "Miku currently plays YouTube videos and playlists through Lavalink. "
    "Use a public YouTube URL or search by song title."
)
SEARCH_UNAVAILABLE_FEEDBACK = "⚠️ Search is temporarily unavailable. Please try again in a moment."
def _unwrap_command_error(error: Exception) -> Exception:
    while isinstance(error, (commands.CommandInvokeError, app_commands.CommandInvokeError)):
        error = error.original
    return error


def _permission_feedback(error: Exception) -> str | None:
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions)):
        permissions = getattr(error, "missing_permissions", ())
        names = ", ".join(permission.replace("_", " ").title() for permission in permissions)
        required = f" ({names})" if names else ""
        return (
            f"🔒 Miku lacks the required Discord permission{'' if len(permissions) == 1 else 's'}"
            f"{required}. Ask a server admin to grant it in this channel or voice channel."
        )
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        permissions = getattr(error, "missing_permissions", ())
        names = ", ".join(permission.replace("_", " ").title() for permission in permissions)
        required = f" ({names})" if names else ""
        return (
            f"🔒 You lack the required Discord permission{'' if len(permissions) == 1 else 's'}"
            f"{required}. Ask a server admin to grant it to your role."
        )
    if isinstance(error, discord.Forbidden):
        return "🔒 Discord denied that action. Ask a server admin to check Miku's channel or voice permissions."
    return None


def _operation_error_feedback(error: Exception, action: str) -> str:
    permission_message = _permission_feedback(error)
    if permission_message:
        return permission_message
    if isinstance(error, (PermissionError, RuntimeError, ValueError, TimeoutError)):
        return str(error)
    return (
        f"⚠️ {action} couldn't be completed. Check Miku's channel or voice permissions "
        "and try again."
    )


def _error_traceback(error: Exception):
    original = _unwrap_command_error(error)
    return original, (type(original), original, original.__traceback__)


def _short(value: str, limit: int = 95) -> str:
    value = discord.utils.escape_mentions(
        discord.utils.escape_markdown(value.replace("\n", " ").strip())
    )
    return value if len(value) <= limit else value[: limit - 1] + "…"


def build_results_embed(query: str, tracks: tuple[Track, ...], warnings: tuple[str, ...] = ()) -> discord.Embed:
    embed = discord.Embed(title=f"Search results: {_short(query, 230)}", color=discord.Color.blurple())
    if tracks:
        first = tracks[0]
        if first.thumbnail_url and first.thumbnail_url.startswith("https://"):
            embed.set_thumbnail(url=first.thumbnail_url)
        for index, track in enumerate(tracks, 1):
            playback = "YouTube audio is loaded by Lavalink when playback starts"
            line = (f"{track.artist or 'Unknown artist/channel'} · {format_duration(track.duration_seconds)} "
                    f"· {track.provider} · {playback}")
            if track.source_url and track.source_url.startswith("https://"):
                line += f"\n[Open source]({track.source_url})"
            embed.add_field(name=f"{index}. {_short(track.title)}", value=line, inline=False)
        embed.set_footer(text=f"Search: {_short(query, 180)}")
    if warnings:
        embed.add_field(name="Search notes", value="\n".join(warnings)[:1000], inline=False)
    return embed


def build_results_embeds(query: str, tracks: tuple[Track, ...],
                         warnings: tuple[str, ...] = (),
                         playable_keys: set[tuple[str, str]] | None = None) -> tuple[discord.Embed, ...]:
    """Build one metadata card per result so each YouTube match can show its thumbnail."""
    embeds: list[discord.Embed] = []
    for index, track in enumerate(tracks[:5], 1):
        album = f"\n**Album:** {_short(track.album, 140)}" if track.album else ""
        release = f" · {track.release_date}" if track.release_date else ""
        if playable_keys is not None:
            playback = (
                "Lavalink will verify audio when requested"
                if track.key in playable_keys else "Lavalink playback is not configured"
            )
        elif track.provider == "youtube":
            playback = "Lavalink resolves audio when playback starts"
        else:
            playback = "Not playable by the current YouTube-only music setup"
        embed = discord.Embed(
            title=f"{index}. {_short(track.title, 250)}",
            description=(
                f"**Artist / channel:** {_short(track.artist or 'Unknown artist/channel', 180)}\n"
                f"**Duration:** {format_duration(track.duration_seconds)}\n"
                f"**Provider:** {track.provider}{album}{release}\n"
                f"**Playback:** {playback}"
            ),
            color=discord.Color.blurple(),
        )
        if track.thumbnail_url and track.thumbnail_url.startswith("https://"):
            embed.set_thumbnail(url=track.thumbnail_url)
        if track.source_url and track.source_url.startswith("https://"):
            embed.add_field(name="Link", value=f"[Open source]({track.source_url})", inline=False)
        embed.add_field(
            name="Official source",
            value="Verified by provider" if track.official_source is True else "Not verified",
            inline=True,
        )
        if index == 1 and warnings:
            embed.add_field(name="Search notes", value="\n".join(warnings)[:1000], inline=False)
        embed.set_footer(text=f"Search: {_short(query, 180)} · Result {index} of {min(len(tracks), 5)}")
        embeds.append(embed)
    return tuple(embeds)


class VoiceConsentView(discord.ui.View):
    def __init__(self, bot: "MikuBot", guild: discord.Guild, member: discord.Member):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild = guild
        self.member = member

    @discord.ui.button(label="I consent — enable listening", style=discord.ButtonStyle.success)
    async def consent(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("Only the requester can enable listening.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        notice = (
            f"{self.member.mention} approved Miku voice commands. Miku is starting short audio-frame "
            "receiving for local wake-word and command recognition; recordings and full transcripts "
            "are not saved. Use `/voice-listening disable` to stop receiving audio."
        )
        if interaction.channel is not None:
            await interaction.channel.send(notice, allowed_mentions=discord.AllowedMentions(users=True))
        try:
            await self.bot.voice_controller.enable(self.guild, self.member)
        except Exception as exc:
            _, traceback = _error_traceback(exc)
            log.error("Voice listening setup failed", exc_info=traceback)
            await interaction.followup.send(
                "⚠️ Voice listening couldn't be enabled. Please check the bot's voice setup and try again.",
                ephemeral=True,
            )
            return
        for item in self.children:
            item.disabled = True
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
        await interaction.followup.send("Listening is on for your short Miku voice commands.", ephemeral=True)


class MikuBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = settings.prefix_commands_enabled
        super().__init__(command_prefix=settings.command_prefix, intents=intents, help_command=None)
        self.settings = settings
        self.database = Database(settings.database_path)
        self.youtube_provider = YouTubeSearchProvider(
            settings.youtube_api_key, playback_enabled=settings.lavalink_configured
        )
        self.providers = ProviderRegistry([self.youtube_provider])
        self.song_service = SongMemoryService(self.database, self.providers)
        self.native_playback = PlaybackManager(self.providers, settings.idle_disconnect_seconds)
        self.youtube_playback = YouTubePlaybackManager(self, settings)
        self.playback = HybridPlaybackManager(
            self.native_playback, self.youtube_playback, self.youtube_provider
        )
        self.voice_controller = VoiceController(self, settings)
        self.voice_parser = VoiceCommandParser(settings.wake_word)
        self._register_commands()

    async def is_owner(self, user: discord.User | discord.Member) -> bool:
        if self.settings.owner_id is not None:
            return user.id == self.settings.owner_id
        return await super().is_owner(user)

    async def setup_hook(self) -> None:
        await self.database.initialize()
        await self.youtube_playback.connect()
        self.add_view(PlayerControlsView(self.playback))
        try:
            await self.sync_application_commands()
        except discord.Forbidden as exc:
            log.error(
                "Slash command sync was denied for the configured destination (%s). "
                "Check DISCORD_GUILD_ID and the bot's access; Miku will continue online without syncing. %s",
                self.settings.guild_id if self.settings.guild_id is not None else "global commands",
                exc,
            )
        except discord.HTTPException as exc:
            log.error(
                "Slash command sync failed (%s). Miku will continue online without syncing: %s",
                self.settings.guild_id if self.settings.guild_id is not None else "global commands",
                exc,
            )

    async def sync_application_commands(self):
        if self.settings.guild_id is not None:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Slash commands synced to configured guild")
        else:
            synced = await self.tree.sync()
            log.info("Global slash commands synced")
        return synced

    async def on_ready(self) -> None:
        log.info("Miku connected as %s", self.user)

    async def on_wavelink_node_ready(self, payload) -> None:
        self.youtube_playback.ready = True
        log.info("Lavalink node is ready")

    async def on_wavelink_node_disconnected(self, payload) -> None:
        self.youtube_playback.ready = False
        log.warning("Lavalink disconnected; YouTube audio playback is temporarily unavailable")

    async def on_wavelink_node_closed(self, node, disconnected) -> None:
        self.youtube_playback.ready = False
        log.warning("Lavalink node closed; queued YouTube audio may need to be restarted")

    async def on_wavelink_track_end(self, payload) -> None:
        await self.playback.on_youtube_track_end(payload)

    async def on_wavelink_track_start(self, payload) -> None:
        await self.youtube_playback.on_track_start(payload)

    async def on_wavelink_track_exception(self, payload) -> None:
        await self.playback.on_youtube_track_exception(payload)

    async def on_wavelink_track_stuck(self, payload) -> None:
        await self.playback.on_youtube_track_stuck(payload)

    async def on_wavelink_websocket_closed(self, payload) -> None:
        await self.youtube_playback.on_websocket_closed(payload)

    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        if (self.user is not None and member.id == self.user.id
                and before.channel is not None and after.channel is None):
            if self.playback.ignores_voice_disconnect(member.guild):
                return
            if member.guild.id in self.voice_controller.active_users:
                try:
                    await self.voice_controller.disable(member.guild)
                except Exception as exc:
                    log.info("Could not reset voice-listening state after disconnect (%s)", type(exc).__name__)
            await self.playback.handle_voice_disconnect(member.guild)

    async def close(self) -> None:
        for guild in list(self.guilds):
            try:
                await self.voice_controller.disable(guild)
            except Exception:
                pass
            try:
                await self.playback.leave(guild)
            except Exception:
                log.info("Could not fully stop voice playback while closing guild %s", guild.id)
        await self.providers.cleanup()
        await self.database.close()
        await self.playback.close()
        await super().close()

    async def ensure_voice(self, member: discord.Member | discord.User, guild: discord.Guild):
        state = getattr(member, "voice", None)
        channel = getattr(state, "channel", None)
        self._validate_voice_channel(guild, channel)
        await self.playback.prepare_youtube(guild, channel, None)
        return self.youtube_playback.state_for(guild.id).player

    @staticmethod
    def _validate_voice_channel(guild: discord.Guild, channel) -> None:
        if channel is None:
            raise RuntimeError("Join a voice channel first.")
        bot_member = getattr(guild, "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if bot_member is not None and callable(permissions_for):
            permissions = permissions_for(bot_member)
            missing = [name for name in ("connect", "speak") if not getattr(permissions, name, False)]
            if missing:
                labels = " and ".join(name.title() for name in missing)
                raise RuntimeError(f"Miku needs the {labels} permission in your voice channel.")

    async def enqueue_track(self, guild: discord.Guild, member: discord.Member | discord.User,
                            track: Track, channel, *, resolved_source=None) -> int:
        """Resolve and queue one exact YouTube result through Lavalink."""
        requester = (
            getattr(member, "display_name", None) or getattr(member, "name", None)
            or f"User {member.id}"
        )
        queued_track = replace(track, requester=requester)
        if queued_track.provider != "youtube":
            raise RuntimeError("Miku currently plays YouTube audio only.")
        voice_channel = getattr(getattr(member, "voice", None), "channel", None)
        self._validate_voice_channel(guild, voice_channel)
        busy = await self.playback.has_active_playback(guild)
        if not busy and guild.id in self.voice_controller.active_users:
            await self.voice_controller.disable(guild)
        if resolved_source is None:
            return await self.playback.enqueue(
                guild, queued_track, channel, voice_channel=voice_channel,
            )
        return await self.playback.enqueue(
            guild, queued_track, channel, resolved_source=resolved_source,
            voice_channel=voice_channel,
        )

    async def enqueue_youtube_items(self, guild: discord.Guild, member: discord.Member | discord.User,
                                    tracks, channel) -> int:
        requester = (
            getattr(member, "display_name", None) or getattr(member, "name", None)
            or f"User {member.id}"
        )
        queued_tracks = [(replace(track, requester=requester), source) for track, source in tracks]
        voice_channel = getattr(getattr(member, "voice", None), "channel", None)
        self._validate_voice_channel(guild, voice_channel)
        busy = await self.playback.has_active_playback(guild)
        if not busy and guild.id in self.voice_controller.active_users:
            await self.voice_controller.disable(guild)
        return await self.playback.enqueue_youtube_playlist(
            guild, queued_tracks, channel, voice_channel=voice_channel,
        )

    def _text_channel(self, guild: discord.Guild, fallback=None):
        return fallback or guild.system_channel

    async def _prefix_text_search(self, ctx: commands.Context, query: str) -> None:
        if ctx.guild is None:
            await ctx.send("Use this command in a server.")
            return
        outcome = await self.song_service.search(query)
        await self._send_search(
            guild=ctx.guild, user=ctx.author, query=query, outcome=outcome, mode="search",
            channel=ctx.channel,
        )

    async def _interaction_text_search(self, interaction: discord.Interaction, query: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        outcome = await self.song_service.search(query)
        await self._interaction_search(interaction, query, outcome, "search")

    async def _send_search(self, *, guild: discord.Guild, user: discord.Member | discord.User,
                           query: str, outcome: SearchOutcome, mode: str, channel,
                           notice: str | None = None, youtube_search: bool = True,
                           result_limit: int = 5):
        if not outcome.tracks:
            content = self._search_feedback(outcome, notice, youtube_search=youtube_search)
            if channel is not None:
                await channel.send(content)
            return None
        tracks = tuple(outcome.tracks[:result_limit])
        embeds = build_results_embeds(
            query, tracks, self._public_search_notes(outcome, youtube_search=youtube_search),
            {track.key for track in tracks if self.providers.supports_playback(track)},
        )
        if notice and embeds:
            embeds[0].description = f"{notice}\n\n{embeds[0].description or ''}"
        return await channel.send(embeds=embeds)

    async def _interaction_search(self, interaction: discord.Interaction, query: str,
                                  outcome: SearchOutcome, mode: str, notice: str | None = None,
                                  youtube_search: bool = True, result_limit: int = 5):
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command must be used in a server.", ephemeral=True)
            return
        if not outcome.tracks:
            message = self._search_feedback(outcome, notice, youtube_search=youtube_search)
            await interaction.followup.send(message, ephemeral=True)
            return
        tracks = tuple(outcome.tracks[:result_limit])
        embeds = build_results_embeds(
            query, tracks, self._public_search_notes(outcome, youtube_search=youtube_search),
            {track.key for track in tracks if self.providers.supports_playback(track)},
        )
        if notice and embeds:
            embeds[0].description = f"{notice}\n\n{embeds[0].description or ''}"
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    def _search_feedback(self, outcome: SearchOutcome, notice: str | None = None,
                         *, youtube_search: bool = True) -> str:
        validation = any(
            "song query between 1 and 200 characters" in warning.casefold()
            for warning in outcome.warnings
        )
        if validation or (notice and notice.casefold().startswith(("enter a song", "song queries must"))):
            return (
                "🎵 Please provide a song name or artist!\n"
                "Usage: `/search <song name>`\n"
                "Example: `/search Kalyani`"
            )

        if youtube_search and (not self.settings.youtube_api_key.strip()
                               and not self.settings.lavalink_configured or any(
            "YOUTUBE_API_KEY is empty" in warning for warning in outcome.warnings
        )):
            status = YOUTUBE_CONFIG_FEEDBACK
        elif any("quota is exhausted" in warning.casefold() for warning in outcome.warnings):
            status = (
                "⚠️ YouTube search has reached its API quota. Try again later or ask the bot owner "
                "to check the YouTube API quota."
            )
        elif outcome.warnings:
            status = SEARCH_UNAVAILABLE_FEEDBACK
        else:
            status = NO_RESULTS_FEEDBACK
        return f"{notice}\n{status}" if notice else status

    def _public_search_notes(self, outcome: SearchOutcome,
                             *, youtube_search: bool = True) -> tuple[str, ...]:
        notes: list[str] = []
        if youtube_search and (not self.settings.youtube_api_key.strip()
                               and not self.settings.lavalink_configured or any(
            "YOUTUBE_API_KEY is empty" in warning for warning in outcome.warnings
        )):
            notes.append(YOUTUBE_CONFIG_FEEDBACK)
        if any("quota is exhausted" in warning.casefold() for warning in outcome.warnings):
            notes.append("YouTube search has reached its API quota and may be unavailable temporarily.")
        elif any("YOUTUBE_API_KEY is empty" not in warning for warning in outcome.warnings):
            notes.append("YouTube search or Lavalink is temporarily unavailable.")
        return tuple(dict.fromkeys(notes))

    @staticmethod
    def _is_url(value: str) -> bool:
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            return "://" in value
        return parsed.scheme.casefold() in {"http", "https", "spotify"} or value.strip().casefold().startswith("spotify:")

    def _play_feedback(self, outcome: SearchOutcome) -> str:
        if any("song query between 1 and 200 characters" in warning.casefold()
               for warning in outcome.warnings):
            return "Please enter a song name or artist between 1 and 200 characters."
        if (not self.settings.lavalink_configured
                and any("YOUTUBE_API_KEY is empty" in warning for warning in outcome.warnings)):
            return (
                "Internet playback isn't configured. For YouTube, set `LAVALINK_URI` and "
                "`LAVALINK_PASSWORD` for an authorized Lavalink v4 server with the YouTube source plugin."
            )
        if any("could not load playable audio" in warning.casefold() for warning in outcome.warnings):
            return (
                "YouTube search found videos, but Lavalink could not load playable audio from them. "
                "Try another public result, or check the Lavalink YouTube source logs."
            )
        if any("quota is exhausted" in warning.casefold() for warning in outcome.warnings):
            return "⚠️ YouTube search has reached its API quota. Try again later or ask the bot owner to check the quota."
        if outcome.warnings and not outcome.tracks:
            return "⚠️ YouTube search or Lavalink is temporarily unavailable. Try again in a moment."
        return (
            "🔎 I couldn't find an eligible playable match. Try another title or artist, "
            "or check the YouTube search results. Miku can play public YouTube audio through Lavalink."
        )

    async def _play_text_command(self, guild: discord.Guild, user: discord.Member | discord.User,
                                 query: str, channel, send_text):
        query_is_url = self._is_url(query)
        if not query.strip() or len(query) > (2048 if query_is_url else 200):
            await send_text("Please enter a song name or artist between 1 and 200 characters.")
            return
        if getattr(getattr(user, "voice", None), "channel", None) is None:
            await send_text("Join a voice channel first, then ask Miku to play a song.")
            return

        if self._is_url(query):
            if self.youtube_provider.is_youtube_url(query):
                try:
                    tracks = await self.youtube_provider.resolve_url(query)
                except ProviderError as exc:
                    await send_text(str(exc))
                    return
                try:
                    position = await self.enqueue_youtube_items(guild, user, tracks, channel)
                except (RuntimeError, discord.HTTPException) as exc:
                    await send_text(_operation_error_feedback(exc, "Playback"))
                    return
                first = tracks[0][0]
                playing_now = await self.playback.is_active_track(guild, first)
                if len(tracks) == 1:
                    response = (
                        f"{'Now playing' if playing_now else f'Queued at position {position}'}: "
                        f"**{_short(first.title, 180)}** · {_short(first.artist or 'YouTube', 100)}."
                    )
                else:
                    response = (
                        f"Added **{len(tracks)} tracks** from the YouTube playlist. "
                        f"First: **{_short(first.title, 180)}**."
                    )
                await send_text(response)
                return
            await send_text(UNSUPPORTED_URL_FEEDBACK)
            return

        try:
            decision = await self.song_service.auto_play_request(guild.id, user.id, query)
        except Exception as exc:
            log.warning("Song search failed during playback (%s)", type(exc).__name__)
            await send_text("⚠️ I couldn't search for a playable match. Please try again in a moment.")
            return
        if decision.track is not None and decision.action == "play_match":
            try:
                position = await self.enqueue_track(
                    guild, user, decision.track, channel, resolved_source=decision.source,
                )
            except (RuntimeError, discord.HTTPException) as exc:
                await send_text(_operation_error_feedback(exc, "Playback"))
                return
            playing_now = await self.playback.is_active_track(guild, decision.track)
            lead = "Now playing" if playing_now else f"Queued at position {position}"
            await send_text(f"{lead}: **{_short(decision.track.title, 180)}**.")
            return
        outcome = decision.search or SearchOutcome(())
        feedback = self._play_feedback(outcome)
        if decision.notice:
            feedback = f"{_short(decision.notice, 500)}\n{feedback}"
        await send_text(feedback)

    async def _play_interaction(self, interaction: discord.Interaction, query: str):
        if interaction.guild is None:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        member = interaction.user
        if getattr(getattr(member, "voice", None), "channel", None) is None:
            await interaction.response.send_message(
                "Join a voice channel first, then ask Miku to play a song.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            async def send_text(content: str):
                await interaction.followup.send(content, ephemeral=True)

            await self._play_text_command(
                interaction.guild, member, query, interaction.channel, send_text,
            )
        except Exception as exc:
            safe_traceback = safe_log_detail(
                "".join(traceback.format_exception(exc)), limit=None, flatten=False,
            )
            log.error(
                "Unexpected playback error in slash command /play (%s):\n%s",
                type(exc).__name__, safe_traceback,
            )
            try:
                await interaction.followup.send(
                    "⚠️ Miku couldn't start playback because of a temporary error. "
                    "Please try again; if it keeps happening, contact the bot owner.",
                    ephemeral=True,
                )
            except (discord.HTTPException, AttributeError):
                log.warning("Could not send the fallback response for slash command /play")

    async def _prefix_play(self, ctx: commands.Context, query: str) -> None:
        if ctx.guild is None:
            await ctx.send("Use this command in a server.")
            return

        await self._play_text_command(
            ctx.guild, ctx.author, query, ctx.channel, ctx.send
        )

    async def _playback_control(self, guild: discord.Guild | None, user, channel, action: str,
                                position: int | None = None, value: str | None = None) -> str:
        if guild is None:
            return "Use this command in a server."
        if action == "queue":
            current, upcoming = await self.playback.queue_snapshot(guild.id)
            return self.playback.queue_summary(current, upcoming)
        if action == "nowplaying":
            current, _ = await self.playback.queue_snapshot(guild.id)
            return "Nothing is playing." if current is None else f"Now playing: {_short(current.title, 180)}"
        denied = self.playback.authorize_control(guild, user)
        if denied:
            return denied
        if action == "previous":
            changed = await self.playback.previous(guild, channel)
            return "Replaying the previous track." if changed else "There isn't a previous track in this session."
        if action == "pause":
            return "Paused playback." if await self.playback.pause(guild) else "Nothing is playing."
        if action == "resume":
            return "Resumed playback." if await self.playback.resume(guild) else "Playback isn't paused."
        if action == "skip":
            return (
                "Skipped to the next track."
                if await self.playback.skip(guild, channel) else "The queue is empty."
            )
        if action == "remove":
            try:
                removed = await self.playback.remove_queued(guild, position or 0)
            except IndexError:
                return "That queue position does not exist."
            return f"Removed **{_short(removed.title, 180)}** from the queue."
        if action == "clear":
            count = await self.playback.clear_upcoming(guild)
            return f"Removed {count} upcoming track(s)."
        if action == "shuffle":
            count = await self.playback.shuffle(guild)
            return f"Shuffled {count} upcoming track(s)."
        if action == "volume":
            if position is None or not 0 <= position <= 100:
                return "Volume must be between 0 and 100."
            await self.playback.set_volume(guild, position)
            return f"Volume set to {position}%."
        if action == "loop":
            if value not in {"off", "track", "queue"}:
                return "Choose loop mode `off`, `track`, or `queue`."
            mode = await self.playback.set_loop(guild, value)
            return f"Loop mode set to **{mode}**."
        if action == "stop":
            await self.playback.stop(guild)
            return "Stopped playback and cleared the queue. Miku remains connected; use `/leave` to disconnect."
        return "That playback control is unavailable."

    async def _voice_channel(self, guild: discord.Guild):
        return self._text_channel(guild)

    async def handle_voice_transcript(self, guild: discord.Guild, member: discord.Member, text: str) -> None:
        command = self.voice_parser.parse(text)
        if command is None:
            return  # ordinary conversation and unsupported phrases stay silent
        channel = await self._voice_channel(guild)
        if command.action == "pause":
            await self.playback.pause(guild)
        elif command.action == "resume":
            await self.playback.resume(guild)
        elif command.action == "skip":
            await self.playback.skip(guild, channel)
        elif command.action == "leave":
            await self.voice_controller.disable(guild)
            await self.playback.leave(guild)
        elif command.action == "queue":
            current, upcoming = await self.playback.queue_snapshot(guild.id)
            if channel:
                summary = "\n".join(f"{i}. {track.title}" for i, track in enumerate(upcoming, 1)) or "Queue is empty."
                if current:
                    summary = f"**Now playing:** {current.title}\n" + summary
                await channel.send(summary[:1900])
        elif command.action in {"play", "search"} and command.query:
            if command.action == "search":
                outcome = await self.song_service.search(command.query)
                await self._send_search(guild=guild, user=member, query=command.query,
                                        outcome=outcome, mode="search", channel=channel)
                return
            decision = await self.song_service.auto_play_request(guild.id, member.id, command.query)
            if decision.action == "play_match" and decision.track is not None:
                try:
                    await self.enqueue_track(
                        guild, member, decision.track, channel, resolved_source=decision.source,
                    )
                    if channel:
                        await channel.send(f"Playing **{decision.track.title}**.")
                except RuntimeError as exc:
                    if channel:
                        await channel.send(_operation_error_feedback(exc, "Playback"))
            else:
                await self._send_search(guild=guild, user=member, query=command.query,
                                        outcome=decision.search or SearchOutcome(()), mode="play",
                                        channel=channel, notice=decision.notice)

    async def _register_voice_group(self) -> None:
        return None

    def _register_commands(self) -> None:
        bot = self

        async def is_bot_owner(interaction: discord.Interaction) -> bool:
            return await bot.is_owner(interaction.user)

        @bot.tree.command(name="sync", description="Sync Miku's slash commands (bot owner only)")
        @app_commands.check(is_bot_owner)
        async def sync_commands(interaction: discord.Interaction):
            synced = await bot.sync_application_commands()
            await interaction.response.send_message(
                f"Synced {len(synced)} slash command(s).", ephemeral=True
            )

        @bot.tree.command(name="join", description="Join your current voice channel")
        async def join(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            try:
                await bot.ensure_voice(interaction.user, interaction.guild)
            except (RuntimeError, discord.HTTPException) as exc:
                await interaction.response.send_message(
                    _operation_error_feedback(exc, "Joining voice"), ephemeral=True
                )
                return
            await interaction.response.send_message("Joined your voice channel.", ephemeral=True)

        @bot.tree.command(name="leave", description="Stop playback and disconnect from voice")
        async def leave(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            denied = bot.playback.authorize_control(interaction.guild, interaction.user)
            if denied:
                await interaction.response.send_message(denied, ephemeral=True)
                return
            await bot.voice_controller.disable(interaction.guild)
            await bot.playback.leave(interaction.guild)
            await interaction.response.send_message("Stopped playback and left voice.", ephemeral=True)

        @bot.tree.command(name="search", description="Search YouTube song and video metadata")
        @app_commands.describe(query="Song, artist, or title")
        @app_commands.checks.cooldown(3, 10.0, key=lambda i: i.user.id)
        async def search(interaction: discord.Interaction, query: str):
            await bot._interaction_text_search(interaction, query)

        @bot.tree.command(name="play", description="Automatically play the best matching result")
        @app_commands.describe(query="Song, artist, or title")
        @app_commands.checks.cooldown(3, 10.0, key=lambda i: i.user.id)
        async def play(interaction: discord.Interaction, query: str):
            await bot._play_interaction(interaction, query)

        @bot.tree.command(name="pause", description="Pause current playback")
        async def pause(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "pause"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="resume", description="Resume paused playback")
        async def resume(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "resume"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="skip", description="Skip the current track")
        async def skip(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "skip"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="previous", description="Replay the last completed track")
        async def previous(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "previous"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="stop", description="Stop playback and clear the queue")
        async def stop(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "stop"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="queue", description="Show the current track and upcoming queue")
        async def queue(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            current, upcoming = await bot.playback.queue_snapshot(interaction.guild.id)
            embed = discord.Embed(title="Miku queue", description=bot.playback.queue_summary(current, upcoming),
                                  color=discord.Color.blurple())
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @bot.tree.command(name="nowplaying", description="Show the current track")
        async def nowplaying(interaction: discord.Interaction):
            current = None
            if interaction.guild:
                current, _ = await bot.playback.queue_snapshot(interaction.guild.id)
            if current:
                await interaction.response.send_message(embed=track_embed(current, title_prefix="Now playing"))
            else:
                await interaction.response.send_message("Nothing is playing.", ephemeral=True)

        @bot.tree.command(name="remove", description="Remove an upcoming queue item by position")
        @app_commands.describe(position="1-based position in the upcoming queue")
        async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1, 100]):
            if interaction.guild is None:
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "remove", position
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="clear", description="Clear upcoming tracks")
        async def clear(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "clear"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="volume", description="Set playback volume from 0 to 100")
        @app_commands.describe(value="Volume percentage")
        async def volume(interaction: discord.Interaction, value: app_commands.Range[int, 0, 100]):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "volume", value
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="loop", description="Set loop mode for the current track or queue")
        @app_commands.choices(mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Track", value="track"),
            app_commands.Choice(name="Queue", value="queue"),
        ])
        async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "loop", value=mode.value
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="shuffle", description="Shuffle upcoming tracks")
        async def shuffle(interaction: discord.Interaction):
            message = await bot._playback_control(
                interaction.guild, interaction.user, interaction.channel, "shuffle"
            )
            await interaction.response.send_message(message, ephemeral=True)

        @bot.tree.command(name="voice-status", description="Show the current voice-listening status")
        async def voice_status(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            await interaction.response.send_message(bot.voice_controller.status(interaction.guild.id), ephemeral=True)

        voice_group = app_commands.Group(name="voice-listening", description="Explicitly enable or disable voice commands")

        @voice_group.command(name="enable", description="Review the notice and explicitly consent to voice listening")
        async def voice_enable(interaction: discord.Interaction):
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            if not bot.settings.voice_commands_enabled:
                await interaction.response.send_message(
                    "Voice commands are disabled. Set VOICE_COMMANDS_ENABLED=true and restart Miku first.",
                    ephemeral=True,
                )
                return
            view = VoiceConsentView(bot, interaction.guild, interaction.user)
            await interaction.response.send_message(
                "Miku will receive short audio frames only after you press the consent button. "
                "Wake-word and command recognition run locally using the configured Vosk model; "
                "recordings and full transcripts are not saved. Other speakers' frames are ignored. "
                "Use `/voice-listening disable` at any time to stop. Continue?",
                view=view, ephemeral=True,
            )

        @voice_group.command(name="disable", description="Immediately stop voice receiving")
        async def voice_disable(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message("Use this command in a server.", ephemeral=True)
                return
            await bot.voice_controller.disable(interaction.guild)
            await interaction.response.send_message("Voice receiving is off.", ephemeral=True)

        bot.tree.add_command(voice_group)

        @bot.tree.command(name="help", description="Show Miku commands and what sources can play")
        async def help_command(interaction: discord.Interaction):
            embed = discord.Embed(
                title="Miku music commands",
                description=(
                    "Miku automatically plays the best-ranked YouTube match. "
                    "YouTube video audio is played through your configured Lavalink v4 server. "
                    "Join a voice channel before requesting playback."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Playback",
                value=("`/play query: Faded` — automatically play the best matching result.\n"
                       "`/play query: https://www.youtube.com/watch?v=VIDEO_ID` — play that exact video."),
                inline=False,
            )
            embed.add_field(
                name="Search",
                value="`/search query: Kalyani` — view read-only metadata results.",
                inline=False,
            )
            embed.add_field(
                name="Queue and player",
                value=("`/queue`, `/nowplaying`, `/pause`, `/resume`, `/skip`, `/previous`, `/stop`, `/leave`, "
                       "`/volume 70`, `/loop off|track|queue`, `/shuffle` — listeners must be in Miku's voice channel."),
                inline=False,
            )
            embed.add_field(
                name="Voice and queue management",
                value=("`/join`, `/remove position: 2`, `/clear`, "
                       "`/voice-status`, `/voice-listening enable`, `/voice-listening disable`, "
                       "`/sync` (bot owner only)."),
                inline=False,
            )
            embed.add_field(
                name="Provider setup",
                value=("YouTube playback needs `LAVALINK_URI` and `LAVALINK_PASSWORD` for an authorized "
                       "Lavalink v4 server with the `youtube-source` plugin."),
                inline=False,
            )
            if bot.settings.prefix_commands_enabled:
                embed.add_field(
                    name="Prefix examples",
                    value=(f"`{bot.settings.command_prefix}p Shape of You` (alias: `"
                           f"{bot.settings.command_prefix}play`) · "
                           f"`{bot.settings.command_prefix}search Kalyani` · "
                           f"`{bot.settings.command_prefix}help`"),
                    inline=False,
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        if self.settings.prefix_commands_enabled:
            self._register_prefix_commands()

    def _register_prefix_commands(self) -> None:
        bot = self

        @bot.command(name="help")
        async def prefix_help(ctx: commands.Context):
            embed = discord.Embed(
                title="Miku music commands",
                description=(
                    "Miku automatically plays the best-ranked YouTube match. "
                    "YouTube video audio uses your configured Lavalink v4 server. Join a voice channel before playback."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Playback",
                value=(f"`{bot.settings.command_prefix}p Faded` — automatically play the best match.\n"
                       f"`{bot.settings.command_prefix}p https://www.youtube.com/watch?v=VIDEO_ID` — play that exact video.\n"
                       f"`{bot.settings.command_prefix}play` remains an alias."),
                inline=False,
            )
            embed.add_field(
                name="Search",
                value=f"`{bot.settings.command_prefix}search Kalyani` — view read-only metadata results.",
                inline=False,
            )
            embed.add_field(
                name="Queue controls",
                value=(f"`{bot.settings.command_prefix}queue`, `{bot.settings.command_prefix}nowplaying`, "
                       f"`{bot.settings.command_prefix}pause`, `{bot.settings.command_prefix}resume`, "
                       f"`{bot.settings.command_prefix}skip`, `{bot.settings.command_prefix}previous`, "
                       f"`{bot.settings.command_prefix}stop`, `{bot.settings.command_prefix}leave`"),
                inline=False,
            )
            embed.add_field(
                name="Voice and queue management",
                value=(f"`{bot.settings.command_prefix}join` · "
                       f"`{bot.settings.command_prefix}remove 2` · "
                       f"`{bot.settings.command_prefix}clear` · "
                       f"`{bot.settings.command_prefix}voice-status` · "
                       f"`{bot.settings.command_prefix}sync` (bot owner only)"),
                inline=False,
            )
            embed.add_field(
                name="Source limits",
                value=("YouTube needs the configured Lavalink v4 server and YouTube source plugin. "
                       "Miku currently plays YouTube audio only."),
                inline=False,
            )
            await ctx.send(embed=embed)

        @bot.command(name="search")
        @commands.cooldown(3, 10, commands.BucketType.user)
        async def prefix_search(ctx: commands.Context, *, query: str):
            await bot._prefix_text_search(ctx, query)

        @bot.command(name="p", aliases=["play"])
        @commands.cooldown(3, 10, commands.BucketType.user)
        async def prefix_play(ctx: commands.Context, *, query: str):
            await bot._prefix_play(ctx, query)

        @bot.command(name="join")
        async def prefix_join(ctx: commands.Context):
            if ctx.guild is None:
                return
            try:
                await bot.ensure_voice(ctx.author, ctx.guild)
                await ctx.send("Joined your voice channel.")
            except (RuntimeError, discord.HTTPException) as exc:
                await ctx.send(_operation_error_feedback(exc, "Joining voice"))

        @bot.command(name="leave")
        async def prefix_leave(ctx: commands.Context):
            if ctx.guild is None:
                await ctx.send("Use this command in a server.")
                return
            denied = bot.playback.authorize_control(ctx.guild, ctx.author)
            if denied:
                await ctx.send(denied)
                return
            await bot.voice_controller.disable(ctx.guild)
            await bot.playback.leave(ctx.guild)
            await ctx.send("Stopped playback and left voice.")

        @bot.command(name="pause")
        async def prefix_pause(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "pause"))

        @bot.command(name="resume")
        async def prefix_resume(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "resume"))

        @bot.command(name="skip")
        async def prefix_skip(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "skip"))

        @bot.command(name="previous")
        async def prefix_previous(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "previous"))

        @bot.command(name="stop")
        async def prefix_stop(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "stop"))

        @bot.command(name="queue")
        async def prefix_queue(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "queue"))

        @bot.command(name="remove")
        async def prefix_remove(ctx: commands.Context, position: int):
            await ctx.send(await bot._playback_control(
                ctx.guild, ctx.author, ctx.channel, "remove", position
            ))

        @bot.command(name="clear")
        async def prefix_clear(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "clear"))

        @bot.command(name="volume")
        async def prefix_volume(ctx: commands.Context, value: int):
            await ctx.send(await bot._playback_control(
                ctx.guild, ctx.author, ctx.channel, "volume", value
            ))

        @bot.command(name="loop")
        async def prefix_loop(ctx: commands.Context, mode: str):
            await ctx.send(await bot._playback_control(
                ctx.guild, ctx.author, ctx.channel, "loop", value=mode.casefold()
            ))

        @bot.command(name="shuffle")
        async def prefix_shuffle(ctx: commands.Context):
            await ctx.send(await bot._playback_control(ctx.guild, ctx.author, ctx.channel, "shuffle"))

        @bot.command(name="nowplaying")
        async def prefix_nowplaying(ctx: commands.Context):
            current = None
            if ctx.guild:
                current, _ = await bot.playback.queue_snapshot(ctx.guild.id)
            await ctx.send(embed=track_embed(current, title_prefix="Now playing") if current else None,
                           content=None if current else "Nothing is playing.")

        @bot.command(name="voice-status")
        async def prefix_voice_status(ctx: commands.Context):
            await ctx.send(bot.voice_controller.status(ctx.guild.id) if ctx.guild else "Use this in a server.")

        @bot.command(name="sync")
        @commands.is_owner()
        async def prefix_sync(ctx: commands.Context):
            synced = await bot.sync_application_commands()
            await ctx.send(f"Synced {len(synced)} slash command(s).")

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        original = _unwrap_command_error(error)
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Please wait {error.retry_after:.1f} seconds before searching again."
        elif isinstance(error, app_commands.CommandNotFound):
            message = "❓ Unknown command. Use `/help` to see Miku's available commands."
        elif permission_message := _permission_feedback(original):
            message = permission_message
        elif isinstance(error, app_commands.CheckFailure):
            message = "🔒 Only the bot owner can use this command."
        else:
            log.error("Unexpected slash command error", exc_info=_error_traceback(error)[1])
            message = (
                "⚠️ I couldn't complete that command just now. Please try again in a moment. "
                "If it keeps happening, contact the bot owner."
            )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = _unwrap_command_error(error)
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Please wait {error.retry_after:.1f} seconds before searching again.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("🔒 Only the bot owner can use this command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            if ctx.command and ctx.command.name == "search":
                message = (
                    "🎵 Please provide a song name or artist!\n"
                    f"Usage: `{ctx.prefix}search <song name>`\n"
                    f"Example: `{ctx.prefix}search Kalyani`"
                )
            else:
                usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
                message = f"Please provide the required argument.\nUsage: `{usage}`"
            await ctx.send(message)
        elif isinstance(error, commands.CommandNotFound):
            prefix = ctx.prefix or self.settings.command_prefix
            await ctx.send(
                f"❓ Unknown command. Use `{prefix}help` or `/help` to see Miku's available commands."
            )
        elif permission_message := _permission_feedback(original):
            await ctx.send(permission_message)
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("🔒 You aren't allowed to use this command.")
        else:
            log.error("Unexpected prefix command error", exc_info=_error_traceback(error)[1])
            await ctx.send(
                "⚠️ I couldn't complete that command just now. Please try again in a moment. "
                "If it keeps happening, contact the bot owner."
            )


def create_bot(settings: Settings | None = None) -> MikuBot:
    return MikuBot(settings or Settings.from_env())


def main() -> int:
    try:
        settings = Settings.from_env()
        settings.validate()
    except (OSError, ValueError) as exc:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        log.error("Miku configuration error: %s", exc)
        return 2

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "Starting Miku (slash-command sync: %s; voice commands: %s)",
        "guild" if settings.guild_id is not None else "global",
        "enabled" if settings.voice_commands_enabled else "disabled",
    )
    log.info(
        "Prefix commands are %s",
        "enabled" if settings.prefix_commands_enabled else "disabled",
    )

    try:
        client = create_bot(settings)
        log.info("Connecting to Discord")
        client.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "Discord rejected DISCORD_BOT_TOKEN. Check the process environment or local .env file."
        )
        return 2
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord requires a privileged intent. Enable Message Content Intent in the Developer Portal, "
            "or disable prefix commands before restarting Miku."
        )
        return 2
    except discord.HTTPException as exc:
        log.error("Discord returned an HTTP error during startup: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Miku stopped by keyboard interrupt")
        return 0
    except Exception:
        log.exception("Miku failed during startup or shutdown")
        return 1

    return 0
