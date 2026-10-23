import asyncio
from types import SimpleNamespace

import discord
import pytest

from miku.models import PlayableSource, Track
from miku.playback import PlaybackManager, PlayerControlsView, track_embed
from miku.providers import MusicProvider, ProviderRegistry


class FileProvider(MusicProvider):
    name = "local"

    async def search(self, query, limit=5):
        return []

    async def resolve(self, track):
        return PlayableSource("file", track.stable_id)

    def supports_playback(self, track):
        return track.provider == "local"


class FakeMessage:
    def __init__(self):
        self.embeds = []
        self.views = []

    async def edit(self, *, embed, view):
        self.embeds.append(embed)
        self.views.append(view)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, embed=None, view=None):
        message = FakeMessage()
        message.embeds.append(embed)
        message.views.append(view)
        self.sent.append((content, embed, view, message))
        return message


class FakeVoice:
    def __init__(self, channel_id=5):
        self.channel = SimpleNamespace(id=channel_id)
        self.playing = False
        self.paused = False
        self.after = None
        self.sources = []
        self.source = None
        self.disconnects = 0

    def is_connected(self):
        return True

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def play(self, source, *, after):
        self.sources.append(source)
        self.source = source
        self.after = after
        self.playing = True
        self.paused = False

    def pause(self):
        self.playing = False
        self.paused = True

    def resume(self):
        self.playing = True
        self.paused = False

    def stop(self):
        self.playing = False
        self.paused = False
        callback, self.after = self.after, None
        if callback:
            callback(None)

    def is_listening(self):
        return False

    async def disconnect(self):
        self.disconnects += 1

    def finish(self):
        self.playing = False
        callback, self.after = self.after, None
        if callback:
            callback(None)


@pytest.fixture
def playback(monkeypatch):
    class FakeAudioSource(discord.AudioSource):
        def read(self):
            return b""

        def is_opus(self):
            return False

        def cleanup(self):
            pass

    monkeypatch.setattr("miku.playback.discord.FFmpegPCMAudio", lambda *a, **k: FakeAudioSource())
    manager = PlaybackManager(ProviderRegistry([FileProvider()]))
    voice = FakeVoice()
    guild = SimpleNamespace(id=1, voice_client=voice)
    channel = FakeChannel()
    return manager, guild, voice, channel


@pytest.mark.asyncio
async def test_player_message_updates_for_queue_pause_resume_and_controls(playback):
    manager, guild, voice, channel = playback
    first = Track("local", "first.mp3", "First Song", "Artist", 180, requester="Alice")
    second = Track("local", "second.mp3", "Second Song", "Artist", 200)

    await manager.enqueue(guild, first, channel)
    await manager.enqueue(guild, second, channel)

    assert voice.disconnects == 0
    assert len(voice.sources) == 1
    assert len(channel.sent) == 1
    display = manager._displays[guild.id]
    assert isinstance(display.view, PlayerControlsView)
    assert display.status == "Playing"
    assert "Playing: First Song" in display.message.embeds[-1].title
    assert "1 upcoming track" in display.message.embeds[-1].footer.text
    fields = {field.name: field.value for field in display.message.embeds[-1].fields}
    assert fields["Requested by"] == "Alice"
    assert "Second Song" in fields["Up next"]
    assert {item.custom_id for item in display.view.children} == {
        "miku:previous", "miku:pause", "miku:resume", "miku:skip", "miku:queue", "miku:loop", "miku:stop"
    }

    assert await manager.pause(guild) is True
    assert voice.is_paused()
    assert display.status == "Paused"
    assert await manager.resume(guild) is True
    assert voice.is_playing()
    assert display.status == "Playing"
    assert len(display.message.embeds) >= 3
    await manager.stop(guild)
    await asyncio.sleep(0)


def test_requester_is_rendered_but_not_persisted_with_track_preferences():
    track = Track("youtube", "AbCdEfGhI_1", "Video", "Channel", requester="Alice")

    embed = track_embed(track, title_prefix="Now playing")

    assert any(field.name == "Requested by" and field.value == "Alice" for field in embed.fields)
    assert "requester" not in track.to_dict()
    assert Track.from_dict(track.to_dict()).requester is None


@pytest.mark.asyncio
async def test_skip_and_previous_advance_the_queue_once(playback):
    manager, guild, voice, channel = playback
    first = Track("local", "first.mp3", "First")
    second = Track("local", "second.mp3", "Second")
    await manager.enqueue(guild, first, channel)
    await manager.enqueue(guild, second, channel)

    assert await manager.skip(guild, channel) is True
    await asyncio.sleep(0)
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert len(voice.sources) == 2

    voice.finish()
    await asyncio.sleep(0.02)
    assert (await manager.queue_for(guild.id).snapshot()) == (None, ())
    assert await manager.previous(guild, channel) is True
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert len(voice.sources) == 3
    await manager.stop(guild)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_controls_require_the_same_active_voice_channel(playback):
    manager, guild, voice, _ = playback
    member = SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(id=99)))

    assert "Join Miku's current voice channel" in manager.authorize_control(guild, member)


@pytest.mark.asyncio
async def test_player_interaction_uses_shared_pause_service_and_denies_other_voice_channels(playback):
    manager, guild, voice, channel = playback
    await manager.enqueue(guild, Track("local", "song.mp3", "Song"), channel)

    class Response:
        def __init__(self):
            self.sent = []

        async def send_message(self, content, *, ephemeral=False):
            self.sent.append((content, ephemeral))

    response = Response()
    member = SimpleNamespace(id=3, voice=SimpleNamespace(channel=SimpleNamespace(id=5)))
    interaction = SimpleNamespace(guild=guild, user=member, channel=channel, response=response)
    await manager.handle_player_interaction(interaction, "pause")
    assert voice.is_paused()
    assert response.sent == [("Paused playback.", True)]

    response.sent.clear()
    member.voice.channel = SimpleNamespace(id=99)
    await manager.handle_player_interaction(interaction, "resume")
    assert voice.is_paused()
    assert response.sent[0][1] is True
    assert "Join Miku's current voice channel" in response.sent[0][0]
    await manager.stop(guild)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_clears_queue_and_player_reports_stopped(playback):
    manager, guild, voice, channel = playback
    track = Track("local", "first.mp3", "First")
    await manager.enqueue(guild, track, channel)

    await manager.stop(guild)

    assert (await manager.queue_for(guild.id).snapshot()) == (None, ())
    assert manager._displays[guild.id].status == "Stopped"
    assert voice.is_connected() is True
    assert "Stopped" in manager._displays[guild.id].message.embeds[-1].title


@pytest.mark.asyncio
async def test_natural_queue_exhaustion_reports_finished_not_stopped(playback):
    manager, guild, voice, channel = playback
    await manager.enqueue(guild, Track("local", "natural-end.mp3", "Natural End"), channel)

    voice.finish()
    await asyncio.sleep(0.02)

    display = manager._displays[guild.id]
    assert display.status == "Finished"
    assert "Finished" in display.message.embeds[-1].title

    await manager.stop(guild)
    assert display.status == "Finished"


@pytest.mark.asyncio
async def test_stale_audio_callback_from_replaced_voice_client_is_ignored(playback):
    manager, guild, old_voice, channel = playback
    track = Track("local", "stale-callback.mp3", "Stale Callback")
    await manager.enqueue(guild, track, channel)
    generation = manager._displays[guild.id].generation
    guild.voice_client = SimpleNamespace()

    await manager._after_audio(
        guild, None, generation, voice_client=old_voice,
    )

    assert (await manager.queue_for(guild.id).snapshot()) == (track, ())


@pytest.mark.asyncio
async def test_native_loop_volume_and_shuffle_controls(playback):
    manager, guild, voice, channel = playback
    first = Track("local", "first.mp3", "First")
    second = Track("local", "second.mp3", "Second")
    third = Track("local", "third.mp3", "Third")
    await manager.enqueue(guild, first, channel)
    await manager.enqueue(guild, second, channel)
    await manager.enqueue(guild, third, channel)

    assert await manager.set_volume(guild, 35) == 35
    assert voice.source.volume == 0.35
    assert await manager.shuffle(guild) == 2
    assert await manager.set_loop(guild, "track") == "track"
    voice.finish()
    await asyncio.sleep(0.02)

    current, _ = await manager.queue_for(guild.id).snapshot()
    assert current == first
    assert len(voice.sources) == 2
    await manager.stop(guild)


@pytest.mark.asyncio
async def test_old_persistent_controls_explain_runtime_queue_was_lost_on_restart(playback):
    manager, guild, _, _ = playback
    manager = PlaybackManager(ProviderRegistry([FileProvider()]))

    class Response:
        def __init__(self):
            self.sent = []

        async def send_message(self, content, *, ephemeral=False):
            self.sent.append((content, ephemeral))

    response = Response()
    interaction = SimpleNamespace(guild=guild, user=SimpleNamespace(id=4), response=response)
    await manager.handle_player_interaction(interaction, "pause")

    assert response.sent[0][1] is True
    assert "ended when Miku restarted" in response.sent[0][0]
