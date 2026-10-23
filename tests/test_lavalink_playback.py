import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
import wavelink

from miku.config import Settings
from miku.lavalink_playback import (
    HybridPlaybackManager, LavalinkGuildQueue, YouTubePlaybackManager, _QueuedTrack,
)
from miku.models import PlayableSource, Track


def _settings(tmp_path):
    return Settings(
        discord_token="", guild_id=None, prefix_commands_enabled=False,
        youtube_api_key="", command_prefix="!", wake_word="miku",
        voice_commands_enabled=False, voice_consent_required=True, voice_language="hi-IN",
        local_music_dir=tmp_path / "music", database_path=tmp_path / "db.sqlite3",
        idle_disconnect_seconds=0, log_level="INFO", vosk_model_path=None,
        voice_wake_confidence=0.75, voice_command_timeout_seconds=5,
        voice_silence_timeout_seconds=0.6, voice_cooldown_seconds=2,
        voice_vad_rms_threshold=250, lavalink_uri="localhost:2333",
        lavalink_password="test-password", lavalink_secure=False,
    )


def _playable(video_id, title):
    return wavelink.Playable({
        "encoded": f"encoded-{video_id}",
        "info": {
            "identifier": video_id, "isSeekable": True, "author": "Uploader",
            "length": 90_000, "isStream": False, "position": 0, "title": title,
            "uri": f"https://www.youtube.com/watch?v={video_id}",
            "artworkUrl": None, "sourceName": "youtube",
        },
        "pluginInfo": {},
    })


class FakePlayer:
    def __init__(self, guild):
        self.guild = guild
        self.channel = SimpleNamespace(id=5)
        self.playing = False
        self.paused = False
        self.position = 12_000
        self.connected = True
        self.current = None
        self.volume = 100
        self.played = []

    async def play(self, track, *, volume):
        self.current = track
        self.played.append(track)
        self.volume = volume
        self.playing = True
        self.paused = False

    async def pause(self, value):
        self.paused = value
        self.playing = not value

    async def skip(self, *, force=True):
        self.playing = False
        return self.current

    async def stop(self):
        self.playing = False

    async def set_volume(self, value):
        self.volume = value

    async def disconnect(self, **kwargs):
        self.connected = False


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, embed=None, view=None):
        message = FakeMessage(embed)
        self.sent.append((content, embed, view, message))
        return message


class FakeMessage:
    def __init__(self, embed):
        self.embed = embed

    async def edit(self, **kwargs):
        if "embed" in kwargs:
            self.embed = kwargs["embed"]


def _message_status(message):
    return next(field.value for field in message.embed.fields if field.name == "Status")


def _channel_sent_content(channel):
    return tuple(content for content, _, _, _ in channel.sent if content)


@pytest.mark.asyncio
async def test_lavalink_queues_are_isolated_and_controls_work(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    guild = SimpleNamespace(id=10)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    assert not hasattr(state.player, "is_connected")
    assert state.player.connected is True
    first = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90,
                  source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1")
    second = Track("youtube", "ZyXwVuTsRq0", "Second", "Uploader", 95,
                   source_url="https://www.youtube.com/watch?v=ZyXwVuTsRq0")
    first_audio, second_audio = _playable(first.stable_id, first.title), _playable(second.stable_id, second.title)

    assert await manager.enqueue(guild, first, channel, PlayableSource("lavalink", first_audio)) == 1
    assert await manager.enqueue(guild, second, channel, PlayableSource("lavalink", second_audio)) == 1
    assert (await manager.queue_for(guild.id).snapshot()) == (first, (second,))
    assert await manager.pause(guild) is True
    assert await manager.resume(guild) is True
    assert await manager.set_volume(guild, 42) == 42
    assert state.player.volume == 42
    assert await manager.set_loop(guild, "queue") == "queue"
    assert await manager.shuffle(guild) == 1
    assert await manager.remove_queued(guild, 1) == second
    assert await manager.clear_upcoming(guild) == 0
    assert (await manager.queue_for(11).snapshot()) == (None, ())


@pytest.mark.asyncio
async def test_enqueue_during_playback_reuses_player_without_voice_lifecycle_calls(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    guild = SimpleNamespace(id=12)
    text_channel = FakeChannel()
    voice_channel = SimpleNamespace(id=5, connect=AsyncMock())
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = text_channel
    player = FakePlayer(guild)
    player.disconnect = AsyncMock()
    player.move_to = AsyncMock()
    player.stop = AsyncMock()
    player.skip = AsyncMock()
    state.player = player
    guild.voice_client = player

    first = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90)
    second = Track("youtube", "ZyXwVuTsRq0", "Second", "Uploader", 95)
    first_audio = _playable(first.stable_id, first.title)
    second_audio = _playable(second.stable_id, second.title)
    await manager.enqueue(guild, first, text_channel, PlayableSource("lavalink", first_audio))
    native = SimpleNamespace(control_handler=None, leave=AsyncMock())
    hybrid = HybridPlaybackManager(native, manager, SimpleNamespace())
    await hybrid.prepare_youtube(guild, voice_channel, text_channel)
    position = await hybrid.enqueue(
        guild, second, text_channel,
        resolved_source=PlayableSource("lavalink", second_audio),
        voice_channel=voice_channel,
    )

    assert position == 1
    assert (await manager.queue_for(guild.id).snapshot()) == (first, (second,))
    assert player.played == [first_audio]
    assert player.current is first_audio
    player.disconnect.assert_not_awaited()
    player.move_to.assert_not_awaited()
    player.stop.assert_not_awaited()
    player.skip.assert_not_awaited()
    voice_channel.connect.assert_not_awaited()
    native.leave.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_youtube_track_is_rejected_without_backend_switch(tmp_path):
    guild = SimpleNamespace(id=120, voice_client=None)
    youtube = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    native = SimpleNamespace(control_handler=None, leave=AsyncMock(), enqueue=AsyncMock())
    hybrid = HybridPlaybackManager(native, youtube, SimpleNamespace())
    channel = SimpleNamespace(id=5, connect=AsyncMock())
    track = Track("jamendo", "legacy-id", "Legacy track")

    assert hybrid.backend_for(guild.id) == "youtube"
    with pytest.raises(RuntimeError, match="YouTube audio only"):
        await hybrid.enqueue(
            guild, track, FakeChannel(), resolved_source=PlayableSource("http", "unused"),
            voice_channel=channel,
        )

    channel.connect.assert_not_awaited()
    native.leave.assert_not_awaited()
    native.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_then_normal_voice_websocket_close_rebuilds_player_on_next_play(tmp_path, monkeypatch):
    class FakeVoicePlayer:
        def __init__(self, guild, channel):
            self.guild = guild
            self.channel = channel
            self.connected = True
            self.playing = True
            self.paused = False
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1
            self.playing = False

        async def disconnect(self, **kwargs):
            self.connected = False
            self.guild.voice_client = None

    monkeypatch.setattr(wavelink, "Player", FakeVoicePlayer)
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    guild = SimpleNamespace(id=17, voice_client=None, me=object())
    channel = SimpleNamespace(id=8)
    old_player = FakeVoicePlayer(guild, channel)
    guild.voice_client = old_player
    state = manager.state_for(guild.id)
    state.guild = guild
    state.player = old_player

    await manager.stop(guild)
    await manager.on_websocket_closed(SimpleNamespace(
        player=old_player, code=wavelink.DiscordVoiceCloseType(1000),
        by_remote=False, reason="normal close after stop",
    ))

    async def connect_player(*, cls):
        player = cls(guild, channel)
        player.playing = False
        guild.voice_client = player
        return player

    channel.connect = AsyncMock(side_effect=connect_player)
    new_player = await manager.ensure_player(guild, channel, None)

    assert old_player.stop_calls == 1
    assert state.reconnect_required is False
    assert new_player is not old_player
    assert state.player is new_player
    channel.connect.assert_awaited_once_with(cls=FakeVoicePlayer)


@pytest.mark.asyncio
async def test_voice_connection_failure_is_actionable_and_sanitized(tmp_path, caplog):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    guild = SimpleNamespace(id=170, voice_client=None, me=None)
    channel = SimpleNamespace(id=8, connect=AsyncMock(side_effect=RuntimeError("token=private-value")))

    with pytest.raises(RuntimeError, match="couldn't connect to your voice channel"):
        await manager.ensure_player(guild, channel, FakeChannel())

    assert "private-value" not in caplog.text
    assert "voice connection failed" in caplog.text


@pytest.mark.asyncio
async def test_reconnect_preserves_upcoming_queue_and_marks_lost_current_failed(tmp_path):
    guild = SimpleNamespace(id=171, voice_client=None)
    text_channel = FakeChannel()
    voice_channel = SimpleNamespace(id=5)
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    native = SimpleNamespace(control_handler=None)
    hybrid = HybridPlaybackManager(native, manager, SimpleNamespace())
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = text_channel
    old_player = FakePlayer(guild)
    old_player.disconnect = AsyncMock(wraps=old_player.disconnect)
    state.player = old_player
    guild.voice_client = old_player
    current = Track("youtube", "AbCdEfGhI_1", "Interrupted", "Uploader", 90)
    queued = Track("youtube", "ZyXwVuTsRq0", "Preserved", "Uploader", 95)
    requested = Track("youtube", "QwErTyUiOp1", "New request", "Uploader", 100)
    current_audio = _playable(current.stable_id, current.title)
    queued_audio = _playable(queued.stable_id, queued.title)
    requested_audio = _playable(requested.stable_id, requested.title)
    await manager.enqueue(guild, current, text_channel, PlayableSource("lavalink", current_audio))
    failed_message = state.display_message
    await manager.enqueue(guild, queued, text_channel, PlayableSource("lavalink", queued_audio))
    state.reconnect_required = True
    new_player = FakePlayer(guild)

    async def connect_player(*, cls):
        guild.voice_client = new_player
        return new_player

    voice_channel.connect = AsyncMock(side_effect=connect_player)
    position = await hybrid.enqueue(
        guild, requested, text_channel,
        resolved_source=PlayableSource("lavalink", requested_audio),
        voice_channel=voice_channel,
    )

    assert position == 1
    assert old_player.disconnect.await_count == 1
    voice_channel.connect.assert_awaited_once()
    assert _message_status(failed_message) == "Failed"
    assert (await state.queue.snapshot()) == (queued, (requested,))
    assert new_player.played == [queued_audio]


@pytest.mark.asyncio
async def test_track_end_advances_once_and_loop_track_restarts_same_item(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    guild = SimpleNamespace(id=20)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    first = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90,
                  source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1")
    second = Track("youtube", "ZyXwVuTsRq0", "Second", "Uploader", 95,
                   source_url="https://www.youtube.com/watch?v=ZyXwVuTsRq0")
    first_audio, second_audio = _playable(first.stable_id, first.title), _playable(second.stable_id, second.title)
    await manager.enqueue(guild, first, channel, PlayableSource("lavalink", first_audio))
    await manager.enqueue(guild, second, channel, PlayableSource("lavalink", second_audio))
    first_message = state.display_message
    ended = SimpleNamespace(
        identifier=state.queue.current_item.playable.identifier,
        extras=dict(state.queue.current_item.playable.extras),
    )
    payload = SimpleNamespace(player=state.player, track=ended, reason="FINISHED")

    await manager.on_track_start(SimpleNamespace(player=state.player, track=ended, original=ended))
    await manager.on_track_end(payload)
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert state.player.played == [first_audio, second_audio]
    assert state.player.current is second_audio
    second_message = state.display_message
    assert first_message is not second_message
    assert _message_status(first_message) == "Finished"
    assert channel.sent[1][3] is second_message
    await manager.on_track_end(payload)
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert state.player.played == [first_audio, second_audio]
    assert len(channel.sent) == 2

    await manager.set_loop(guild, "track")
    second_ended_message = state.display_message
    second_ended = SimpleNamespace(
        identifier=state.queue.current_item.playable.identifier,
        extras=dict(state.queue.current_item.playable.extras),
    )
    old_token = manager._payload_token(second_ended)
    await manager.on_track_start(SimpleNamespace(
        player=state.player, track=second_ended, original=second_ended,
    ))
    await manager.on_track_end(SimpleNamespace(player=state.player, track=second_ended, reason="FINISHED"))
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert manager._payload_token(state.queue.current_item.playable) != old_token
    assert _message_status(second_ended_message) == "Finished"
    assert state.display_message is not second_ended_message
    assert len(channel.sent) == 3


@pytest.mark.asyncio
async def test_unstarted_track_end_is_failed_and_empty_queue_stays_empty(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=200)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    track = Track("youtube", "AbCdEfGhI_1", "Never started", "Uploader", 90)
    audio = _playable(track.stable_id, track.title)
    await manager.enqueue(guild, track, channel, PlayableSource("lavalink", audio))
    message = state.display_message
    event = SimpleNamespace(identifier=audio.identifier, extras=dict(audio.extras))

    await manager.on_track_end(SimpleNamespace(player=state.player, track=event, reason="FINISHED"))

    assert (await state.queue.snapshot()) == (None, ())
    assert _message_status(message) == "Failed"
    assert state.display_message is None
    assert len(channel.sent) == 2  # One player card and one failure notice.


@pytest.mark.asyncio
async def test_skip_finishes_previous_message_and_posts_next_track_message(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=201)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    first = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90)
    second = Track("youtube", "ZyXwVuTsRq0", "Second", "Uploader", 95)
    await manager.enqueue(
        guild, first, channel,
        PlayableSource("lavalink", _playable(first.stable_id, first.title)),
    )
    await manager.enqueue(
        guild, second, channel,
        PlayableSource("lavalink", _playable(second.stable_id, second.title)),
    )
    first_message = state.display_message
    ended = SimpleNamespace(
        identifier=state.queue.current_item.playable.identifier,
        extras=dict(state.queue.current_item.playable.extras),
    )

    assert await manager.skip(guild) is True
    await manager.on_track_end(SimpleNamespace(
        player=state.player, track=ended, reason="STOPPED",
    ))

    assert _message_status(first_message) == "Skipped"
    assert state.display_message is channel.sent[1][3]
    assert state.display_message is not first_message
    assert _message_status(state.display_message) == "Loading"
    assert state.display_message.embed.title.startswith("Loading: Second")
    assert state.player.played[-1].identifier == second.stable_id
    assert len(channel.sent) == 2


@pytest.mark.asyncio
async def test_stop_marks_current_message_stopped_without_creating_an_idle_duplicate(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=202)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    current = Track("youtube", "AbCdEfGhI_1", "Current", "Uploader", 90)
    following = Track("youtube", "ZyXwVuTsRq0", "Following", "Uploader", 95)
    await manager.enqueue(
        guild, current, channel,
        PlayableSource("lavalink", _playable(current.stable_id, current.title)),
    )
    await manager.enqueue(
        guild, following, channel,
        PlayableSource("lavalink", _playable(following.stable_id, following.title)),
    )
    current_message = state.display_message

    await manager.stop(guild)

    assert _message_status(current_message) == "Stopped"
    assert state.display_message is None
    assert await manager.queue_for(guild.id).snapshot() == (None, ())
    assert len(channel.sent) == 1
    await manager.on_track_end(SimpleNamespace(
        player=state.player,
        track=SimpleNamespace(identifier=current.stable_id, extras={}),
        reason="STOPPED",
    ))
    assert state.display_message is None
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_track_failure_advances_queue_and_late_end_event_is_ignored(tmp_path, monkeypatch):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    manager.ready = True
    guild = SimpleNamespace(id=21)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    first = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90,
                  source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1")
    second = Track("youtube", "ZyXwVuTsRq0", "Second", "Uploader", 95,
                   source_url="https://www.youtube.com/watch?v=ZyXwVuTsRq0")
    await manager.enqueue(guild, first, channel, PlayableSource("lavalink", _playable(first.stable_id, first.title)))
    await manager.enqueue(guild, second, channel, PlayableSource("lavalink", _playable(second.stable_id, second.title)))
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(wavelink.Playable, "search", search)
    failed = SimpleNamespace(
        identifier=state.queue.current_item.playable.identifier,
        extras=dict(state.queue.current_item.playable.extras),
    )

    await manager.on_track_failure(SimpleNamespace(player=state.player, track=failed))
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert len(state.player.played) == 2
    assert any("Lavalink couldn't load this YouTube track" in content
               for content in _channel_sent_content(channel))
    assert _message_status(channel.sent[0][3]) == "Failed"
    search.assert_not_awaited()

    await manager.on_track_end(SimpleNamespace(player=state.player, track=failed, reason="loadFailed"))
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert len(state.player.played) == 2


@pytest.mark.asyncio
async def test_youtube_load_failure_marks_failed_and_advances_without_provider_fallback(tmp_path, monkeypatch):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=211)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    first = Track("youtube", "AbCdEfGhI_1", "First Song", "Uploader", 90)
    second = Track("youtube", "ZyXwVuTsRq0", "Second Song", "Uploader", 95)
    first_audio = _playable(first.stable_id, first.title)
    second_audio = _playable(second.stable_id, second.title)
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(wavelink.Playable, "search", search)
    await manager.enqueue(guild, first, channel, PlayableSource("lavalink", first_audio))
    first_message = state.display_message
    await manager.enqueue(guild, second, channel, PlayableSource("lavalink", second_audio))
    failed = SimpleNamespace(identifier=first_audio.identifier, extras=dict(first_audio.extras))

    await manager.on_track_exception(SimpleNamespace(
        player=state.player, track=failed,
        exception={"severity": "COMMON", "cause": "No YouTube stream", "message": ""},
    ))

    assert (await state.queue.snapshot()) == (second, ())
    assert state.player.played == [first_audio, second_audio]
    assert _message_status(first_message) == "Failed"
    assert _message_status(state.display_message) == "Loading"
    assert any("Lavalink couldn't load this YouTube track" in content
               for content in _channel_sent_content(channel))
    search.assert_not_awaited()

    # The paired loadFailed end callback for the same token is stale after advancing.
    await manager.on_track_end(SimpleNamespace(player=state.player, track=failed, reason="loadFailed"))
    assert (await state.queue.snapshot()) == (second, ())
    assert state.player.played == [first_audio, second_audio]


@pytest.mark.asyncio
async def test_player_play_exception_marks_its_message_failed_and_continues(tmp_path, caplog):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=212)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    first = Track("youtube", "AbCdEfGhI_1", "First Song", "Uploader", 90)
    second = Track("youtube", "ZyXwVuTsRq0", "Second Song", "Uploader", 95)
    first_audio = _playable(first.stable_id, first.title)
    second_audio = _playable(second.stable_id, second.title)
    original_play = state.player.play

    async def play_with_first_failure(track, *, volume):
        if track is first_audio:
            raise RuntimeError("password=do-not-log")
        await original_play(track, volume=volume)

    state.player.play = play_with_first_failure
    await manager.enqueue(guild, first, channel, PlayableSource("lavalink", first_audio))
    first_message = channel.sent[0][3]
    await manager.enqueue(guild, second, channel, PlayableSource("lavalink", second_audio))

    assert (await state.queue.snapshot()) == (second, ())
    assert state.player.played == [second_audio]
    assert _message_status(first_message) == "Failed"
    assert _message_status(state.display_message) == "Loading"
    assert "do-not-log" not in caplog.text
    assert any("unavailable or restricted" in content for content in _channel_sent_content(channel))


@pytest.mark.asyncio
async def test_same_video_stale_callback_cannot_advance_repeated_queue_item(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=213)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    repeated = Track("youtube", "AbCdEfGhI_1", "Repeated Song", "Uploader", 90)
    first_audio = _playable(repeated.stable_id, repeated.title)
    second_audio = _playable(repeated.stable_id, repeated.title)
    await manager.enqueue(guild, repeated, channel, PlayableSource("lavalink", first_audio))
    first_message = state.display_message
    await manager.enqueue(guild, repeated, channel, PlayableSource("lavalink", second_audio))
    first_event_track = SimpleNamespace(
        identifier=first_audio.identifier, extras=dict(state.queue.current_item.playable.extras),
    )
    await manager.on_track_start(SimpleNamespace(
        player=state.player, track=first_event_track, original=first_event_track,
    ))
    original_token = manager._payload_token(first_event_track)
    await manager.on_track_end(SimpleNamespace(
        player=state.player, track=first_event_track, reason="FINISHED",
    ))
    second = state.queue.current_item
    second_token = second.token
    await manager.on_track_end(SimpleNamespace(
        player=state.player, track=first_event_track, reason="FINISHED",
    ))

    assert _message_status(first_message) == "Finished"
    assert second.token == second_token
    assert second.token != original_token
    assert state.queue.current_item is second
    assert state.player.played == [first_audio, second_audio]
    assert len(channel.sent) == 2


@pytest.mark.asyncio
async def test_track_is_loading_until_lavalink_confirms_track_start(tmp_path):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=22)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    track = Track(
        "youtube", "AbCdEfGhI_1", "First", "Uploader", 90,
        source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1",
    )
    await manager.enqueue(
        guild, track, channel,
        PlayableSource("lavalink", _playable(track.stable_id, track.title)),
    )

    assert state.player.playing is True  # Wavelink sets this before Lavalink confirms playback.
    assert await manager.is_active_track(guild, track) is False
    loading_embed = channel.sent[-1][1]
    assert loading_embed.title.startswith("Loading:")
    assert next(field.value for field in loading_embed.fields if field.name == "Status") == "Loading"

    event_track = SimpleNamespace(
        identifier=state.queue.current_item.playable.identifier,
        extras=dict(state.queue.current_item.playable.extras),
    )
    await manager.on_track_start(SimpleNamespace(
        player=state.player, track=event_track, original=event_track,
    ))

    assert await manager.is_active_track(guild, track) is True
    playing_embed = state.display_message.embed
    assert playing_embed.title.startswith("Now playing:")
    assert next(field.value for field in playing_embed.fields if field.name == "Status") == "Playing"


@pytest.mark.asyncio
async def test_track_exception_logs_sanitized_cause_and_advances_only_matching_track(
    tmp_path, caplog, monkeypatch,
):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=23)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    first = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90)
    second = Track("youtube", "ZyXwVuTsRq0", "Second", "Uploader", 95)
    await manager.enqueue(
        guild, first, channel, PlayableSource("lavalink", _playable(first.stable_id, first.title)),
    )
    await manager.enqueue(
        guild, second, channel, PlayableSource("lavalink", _playable(second.stable_id, second.title)),
    )
    monkeypatch.setattr(wavelink.Playable, "search", AsyncMock(return_value=[]))
    failed = SimpleNamespace(
        identifier=state.queue.current_item.playable.identifier,
        extras=dict(state.queue.current_item.playable.extras),
    )
    caplog.set_level(logging.WARNING, logger="miku.lavalink_playback")

    await manager.on_track_exception(SimpleNamespace(
        player=state.player,
        track=failed,
        exception={
            "severity": "COMMON",
            "cause": "All clients failed to load the item",
            "message": "login required https://youtube.invalid/watch?v=x token=secret-value",
        },
    ))

    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert len(state.player.played) == 2
    assert "TrackException" in caplog.text
    assert "All clients failed to load the item" in caplog.text
    assert "login required" in caplog.text
    assert "youtube.invalid" not in caplog.text
    assert "secret-value" not in caplog.text
    assert any("Lavalink couldn't load this YouTube track" in content
               for content in _channel_sent_content(channel))

    # A delayed failure for the previous track must not skip the new current track.
    await manager.on_track_exception(SimpleNamespace(
        player=state.player, track=failed,
        exception={"severity": "COMMON", "cause": "duplicate", "message": ""},
    ))
    assert (await manager.queue_for(guild.id).snapshot()) == (second, ())
    assert len(state.player.played) == 2


@pytest.mark.asyncio
async def test_track_stuck_is_logged_as_stuck_and_advances_queue(tmp_path, caplog):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=24)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    track = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90)
    await manager.enqueue(
        guild, track, channel, PlayableSource("lavalink", _playable(track.stable_id, track.title)),
    )
    current = state.queue.current_item
    event_track = SimpleNamespace(
        identifier=current.playable.identifier,
        extras=dict(current.playable.extras),
    )
    caplog.set_level(logging.WARNING, logger="miku.lavalink_playback")

    await manager.on_track_stuck(SimpleNamespace(player=state.player, track=event_track, threshold=10_000))

    assert (await manager.queue_for(guild.id).snapshot()) == (None, ())
    assert "TrackStuck" in caplog.text
    assert "threshold_ms=10000" in caplog.text
    assert "stalled" in channel.sent[-1][0]
    assert _message_status(channel.sent[0][3]) == "Failed"


@pytest.mark.asyncio
async def test_voice_websocket_close_is_diagnosed_without_dropping_queue(tmp_path, caplog):
    manager = YouTubePlaybackManager(SimpleNamespace(), _settings(tmp_path))
    guild = SimpleNamespace(id=25)
    channel = FakeChannel()
    state = manager.state_for(guild.id)
    state.guild = guild
    state.channel = channel
    state.player = FakePlayer(guild)
    track = Track("youtube", "AbCdEfGhI_1", "First", "Uploader", 90)
    await manager.enqueue(
        guild, track, channel, PlayableSource("lavalink", _playable(track.stable_id, track.title)),
    )
    caplog.set_level(logging.WARNING, logger="miku.lavalink_playback")

    await manager.on_websocket_closed(SimpleNamespace(
        player=state.player, code=4006, by_remote=True, reason="transport closed",
    ))

    assert (await manager.queue_for(guild.id).snapshot()) == (track, ())
    assert len(state.player.played) == 1
    assert "voice websocket closed" in caplog.text
    assert "code=4006" in caplog.text


@pytest.mark.asyncio
async def test_youtube_without_reachable_lavalink_has_actionable_feedback(tmp_path):
    settings = _settings(tmp_path)
    manager = YouTubePlaybackManager(SimpleNamespace(), settings)
    guild = SimpleNamespace(id=30)
    with pytest.raises(RuntimeError, match="LAVALINK_URI"):
        await manager.ensure_player(guild, SimpleNamespace(id=5), FakeChannel())


@pytest.mark.asyncio
async def test_queue_remove_and_previous_preserve_exact_track_objects():
    queue = LavalinkGuildQueue()
    a = Track("youtube", "a", "A")
    b = Track("youtube", "b", "B")
    await queue.enqueue_many([
        _QueuedTrack(a, _playable("AbCdEfGhI_1", "A"), "a-token"),
        _QueuedTrack(b, _playable("ZyXwVuTsRq0", "B"), "b-token"),
    ])
    assert (await queue.snapshot()) == (None, (a, b))
    assert await queue.remove(2) == b
    assert await queue.start_next() is not None
    assert await queue.finish_current() is not None
    assert await queue.prepare_previous() is True
    assert (await queue.snapshot()) == (None, (a,))
