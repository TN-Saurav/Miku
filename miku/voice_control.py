from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .voice import VoskAudioPipeline, make_voice_sink

log = logging.getLogger(__name__)


class VoiceController:
    def __init__(self, bot, settings):
        self.bot = bot
        self.settings = settings
        self.active_users: dict[int, int] = {}
        self.pipeline: VoskAudioPipeline | None = None
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def status(self, guild_id: int) -> str:
        if guild_id in self.active_users:
            return f"Listening is enabled for user {self.active_users[guild_id]} (wake word: {self.settings.wake_word})."
        if not self.settings.voice_commands_enabled:
            return "Voice commands are disabled by configuration. Text commands are available."
        if self.settings.vosk_model_path is None:
            return "Voice commands are enabled but VOSK_MODEL_PATH is not configured."
        try:
            import discord.ext.voice_recv  # noqa: F401
        except Exception:
            return "Voice receive support is not installed. Text commands are available."
        return (f"Voice commands are off. Model language: {self.settings.voice_language}. "
                "Use /voice-listening enable and accept the listening notice.")

    async def enable(self, guild, member) -> None:
        async with self._locks[guild.id]:
            if guild.id in self.active_users:
                raise RuntimeError("Voice listening is already enabled in this server.")
            await self._enable_locked(guild, member)

    async def _enable_locked(self, guild, member) -> None:
        if not self.settings.voice_commands_enabled:
            raise RuntimeError("Set VOICE_COMMANDS_ENABLED=true before enabling voice commands.")
        if self.settings.vosk_model_path is None:
            raise RuntimeError("Set VOSK_MODEL_PATH to an installed local Vosk model first.")
        if member.voice is None or member.voice.channel is None:
            raise RuntimeError("Join a voice channel before enabling voice commands.")
        try:
            from discord.ext import voice_recv
        except Exception as exc:
            raise RuntimeError(
                'Install optional voice dependencies with `python -m pip install -e ".[voice]"` '
                "from the project root."
            ) from exc

        voice_client = guild.voice_client
        if voice_client is not None and not isinstance(voice_client, voice_recv.VoiceRecvClient):
            raise RuntimeError("Use /leave first so Miku can reconnect with voice receiving enabled.")
        if voice_client is None:
            voice_client = await member.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        elif voice_client.channel.id != member.voice.channel.id:
            await voice_client.move_to(member.voice.channel)

        loop = asyncio.get_running_loop()
        permitted_user_id = member.id

        def on_command(speaker_id: int, text: str) -> None:
            if speaker_id != permitted_user_id:
                return

            async def dispatch() -> None:
                try:
                    await self.bot.handle_voice_transcript(guild, member, text)
                except Exception as exc:
                    log.warning("Voice command handler failed: %s", type(exc).__name__)

            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(dispatch())
            )

        try:
            pipeline = await asyncio.to_thread(
                VoskAudioPipeline,
                self.settings.vosk_model_path,
                on_command,
                wake_word=self.settings.wake_word,
                wake_confidence=self.settings.voice_wake_confidence,
                vad_rms_threshold=self.settings.voice_vad_rms_threshold,
                max_capture_frames=self.settings.voice_command_timeout_seconds * 50,
                silence_timeout_seconds=self.settings.voice_silence_timeout_seconds,
                cooldown_seconds=self.settings.voice_cooldown_seconds,
            )
            sink = make_voice_sink(
                lambda speaker_id, pcm: pipeline.feed(speaker_id, pcm)
                if speaker_id == permitted_user_id else None
            )
            voice_client.listen(sink)
        except Exception:
            if voice_client is not None and not voice_client.is_playing():
                await voice_client.disconnect()
            raise
        self.pipeline = pipeline
        self.active_users[guild.id] = member.id
        await self.bot.database.set_guild_config(guild.id, "voice_listening", "enabled")

    async def disable(self, guild) -> bool:
        async with self._locks[guild.id]:
            was_active = self.active_users.pop(guild.id, None) is not None
            voice_client = guild.voice_client
            if voice_client and hasattr(voice_client, "stop_listening"):
                voice_client.stop_listening()
            self.pipeline = None
            await self.bot.database.set_guild_config(guild.id, "voice_listening", "disabled")
            return was_active
