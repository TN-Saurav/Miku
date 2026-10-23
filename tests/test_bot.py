from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.ext import commands

from miku.bot import build_results_embeds, create_bot
from miku.config import Settings
from miku.models import PlayDecision, PlayableSource, SearchOutcome, Track


@pytest.mark.asyncio
async def test_wavelink_events_dispatch_to_distinct_playback_handlers(tmp_path):
    bot = create_bot(_settings(tmp_path))
    payload = object()
    bot.youtube_playback.on_track_start = AsyncMock()
    bot.youtube_playback.on_track_end = AsyncMock()
    bot.youtube_playback.on_track_exception = AsyncMock()
    bot.youtube_playback.on_track_stuck = AsyncMock()
    bot.youtube_playback.on_websocket_closed = AsyncMock()

    await bot.on_wavelink_track_start(payload)
    await bot.on_wavelink_track_end(payload)
    await bot.on_wavelink_track_exception(payload)
    await bot.on_wavelink_track_stuck(payload)
    await bot.on_wavelink_websocket_closed(payload)

    bot.youtube_playback.on_track_start.assert_awaited_once_with(payload)
    bot.youtube_playback.on_track_end.assert_awaited_once_with(payload)
    bot.youtube_playback.on_track_exception.assert_awaited_once_with(payload)
    bot.youtube_playback.on_track_stuck.assert_awaited_once_with(payload)
    bot.youtube_playback.on_websocket_closed.assert_awaited_once_with(payload)


def test_voice_feature_disabled_does_not_remove_text_commands(tmp_path):
    bot = create_bot(_settings(tmp_path))
    assert bot.get_command("p") is not None
    assert bot.get_command("play") is not None
    assert bot.get_command("p") is bot.get_command("play")
    assert bot.get_command("search") is not None
    assert bot.get_command("reselect") is None
    assert bot.get_command("select-play") is None
    assert bot.get_command("forget-song") is None
    assert bot.get_command("previous") is not None
    assert bot.voice_controller.status(1).startswith("Voice commands are disabled")
    assert bot.tree.get_command("play") is not None
    assert bot.tree.get_command("reselect") is None
    assert bot.tree.get_command("select-play") is None
    assert bot.tree.get_command("forget-song") is None
    assert bot.tree.get_command("volume") is not None
    assert bot.tree.get_command("loop") is not None
    assert bot.tree.get_command("shuffle") is not None
    assert bot.tree.get_command("previous") is not None
    assert bot.get_command("sync") is not None
    assert bot.get_command("volume") is not None
    assert bot.get_command("loop") is not None
    assert bot.get_command("shuffle") is not None
    assert bot.tree.get_command("sync") is not None
    voice_group = bot.tree.get_command("voice-listening")
    assert {command.name for command in voice_group.commands} == {"enable", "disable"}
    for command in bot.tree.get_commands():
        assert command.to_dict(bot.tree)["name"] == command.name


@pytest.mark.asyncio
async def test_prefix_p_is_primary_and_play_remains_an_alias(tmp_path):
    bot = create_bot(_settings(tmp_path))
    track = Track("youtube", "AbCdEfGhI_1", "Faded", "Artist")
    source = PlayableSource("lavalink", object())
    bot.song_service.auto_play_request = AsyncMock(
        return_value=PlayDecision("play_match", track, source=source)
    )
    bot.enqueue_track = AsyncMock(return_value=1)
    bot.playback.is_active_track = AsyncMock(return_value=True)
    author = SimpleNamespace(id=2, voice=SimpleNamespace(channel=SimpleNamespace(id=4)))
    channel = _Channel(3)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1), author=author, channel=channel, send=AsyncMock(),
    )

    assert bot.get_command("p") is bot.get_command("play")
    await bot.get_command("p").callback(ctx, query="Faded")

    bot.song_service.auto_play_request.assert_awaited_once_with(ctx.guild.id, author.id, "Faded")
    bot.enqueue_track.assert_awaited_once_with(
        ctx.guild, author, track, channel, resolved_source=source,
    )
    ctx.send.assert_awaited_once_with("Now playing: **Faded**.")


def test_search_embed_describes_youtube_lavalink_resolution():
    from miku.bot import build_results_embed

    youtube = Track("youtube", "AbCdEfGhI_1", "Video", "Channel",
                    thumbnail_url="https://i.ytimg.com/vi/x/default.jpg",
                    source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1")
    embed = build_results_embed("song", (youtube,))
    values = " ".join(field.value for field in embed.fields)
    assert "YouTube audio is loaded by Lavalink" in values
    assert "Jamendo" not in values


def test_result_cards_are_read_only_and_show_thumbnail_channel_duration_and_link():
    tracks = (
        Track("youtube", "AbCdEfGhI_1", "First Song", "Channel A", 187,
              "https://i.ytimg.com/vi/first/default.jpg",
              "https://www.youtube.com/watch?v=AbCdEfGhI_1"),
        Track("youtube", "ZyXwVuTsRq0", "Second Song", "Channel B", 205,
              "https://i.ytimg.com/vi/second/default.jpg",
              "https://www.youtube.com/watch?v=ZyXwVuTsRq0"),
    )

    embeds = build_results_embeds("Kalyani", tracks)

    assert len(embeds) == 2
    assert embeds[0].title == "1. First Song"
    assert embeds[0].thumbnail.url == tracks[0].thumbnail_url
    assert "Channel A" in embeds[0].description
    assert "3:07" in embeds[0].description
    assert "youtube.com/watch?v=AbCdEfGhI_1" in embeds[0].fields[0].value
    assert embeds[1].title == "2. Second Song"
    assert embeds[1].thumbnail.url == tracks[1].thumbnail_url
    assert "Channel B" in embeds[1].description
    assert "3:25" in embeds[1].description

    assert "Select" not in (embeds[0].footer.text or "")


def test_test_settings_use_no_credentials_or_generated_files(tmp_path):
    assert not (tmp_path / ".env").exists()
    assert Path(".env.example").exists()


def _settings(tmp_path, *, guild_id=None, prefix_commands_enabled=True, youtube_api_key="",
              jamendo_client_id="", owner_id=None):
    return Settings(
        discord_token="", guild_id=guild_id,
        prefix_commands_enabled=prefix_commands_enabled,
        youtube_api_key=youtube_api_key, command_prefix="!",
        wake_word="miku", voice_commands_enabled=False, voice_consent_required=True,
        voice_language="hi-IN", local_music_dir=tmp_path / "music",
        database_path=tmp_path / "db.sqlite3", idle_disconnect_seconds=0, log_level="INFO",
        vosk_model_path=None, voice_wake_confidence=0.75,
        voice_command_timeout_seconds=5, voice_silence_timeout_seconds=0.6,
        voice_cooldown_seconds=2, voice_vad_rms_threshold=250,
        jamendo_client_id=jamendo_client_id,
        owner_id=owner_id,
    )


def test_prefix_commands_can_be_disabled_without_affecting_slash_commands(tmp_path):
    settings = _settings(tmp_path, prefix_commands_enabled=False)
    bot = create_bot(settings)

    assert bot.intents.message_content is False
    assert bot.get_command("p") is None
    assert bot.get_command("play") is None
    assert bot.tree.get_command("play") is not None


class _Channel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_prefix_commands_work_in_multiple_channels_and_ignore_ordinary_messages(tmp_path):
    bot = create_bot(_settings(tmp_path, youtube_api_key="test-api-key"))
    await bot.database.initialize()
    bot._connection.user = SimpleNamespace(id=999)
    bot.song_service.search = AsyncMock(return_value=SearchOutcome(()))
    guild = SimpleNamespace(id=1)

    for channel_id in (10, 20):
        channel = _Channel(channel_id)
        author = SimpleNamespace(id=channel_id + 100, bot=False)
        message = SimpleNamespace(
            content="!search test song", author=author, channel=channel, guild=guild,
            _state=bot._connection,
        )
        ctx = await bot.get_context(message)
        assert ctx.command is bot.get_command("search")
        await ctx.command.callback(ctx, query="test song")
        assert channel.sent == [(
            "🔎 No matching songs were found. Try another song title or artist.", {}
        )]

    ordinary_channel = _Channel(30)
    ordinary = SimpleNamespace(
        content="hello Miku", author=SimpleNamespace(id=300, bot=False),
        channel=ordinary_channel, guild=guild, _state=bot._connection,
    )
    ordinary_ctx = await bot.get_context(ordinary)
    assert ordinary_ctx.command is None
    assert not ordinary_ctx.valid
    await bot.process_commands(ordinary)
    assert ordinary_channel.sent == []

    unknown_channel = _Channel(40)
    unknown_command = SimpleNamespace(
        content="!not-registered", author=SimpleNamespace(id=400, bot=False),
        channel=unknown_channel, guild=guild, _state=bot._connection,
    )
    unknown_ctx = await bot.get_context(unknown_command)
    assert unknown_ctx.command is None
    error_ctx = SimpleNamespace(prefix="!", send=AsyncMock())
    await bot.on_command_error(error_ctx, commands.CommandNotFound("not-registered"))
    error_ctx.send.assert_awaited_once_with(
        "❓ Unknown command. Use `!help` or `/help` to see Miku's available commands."
    )
    assert unknown_channel.sent == []
    await bot.database.close()


@pytest.mark.asyncio
async def test_slash_search_runs_in_multiple_channels(tmp_path):
    bot = create_bot(_settings(tmp_path, youtube_api_key="test-api-key"))
    await bot.database.initialize()
    bot.song_service.search = AsyncMock(return_value=SearchOutcome(()))
    command = bot.tree.get_command("search")

    for channel_id in (10, 20):
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=channel_id + 100),
            channel=_Channel(channel_id),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=followup,
        )
        await command.callback(interaction, query="test song")
        followup.send.assert_awaited_once_with(
            "🔎 No matching songs were found. Try another song title or artist.", ephemeral=True
        )
    await bot.database.close()


@pytest.mark.asyncio
async def test_missing_prefix_search_query_gets_usage_and_example(tmp_path):
    bot = create_bot(_settings(tmp_path))
    command = bot.get_command("search")
    ctx = SimpleNamespace(prefix="!", command=command, send=AsyncMock())
    error = commands.MissingRequiredArgument(command.clean_params["query"])

    await bot.on_command_error(ctx, error)

    ctx.send.assert_awaited_once_with(
        "🎵 Please provide a song name or artist!\n"
        "Usage: `!search <song name>`\n"
        "Example: `!search Kalyani`"
    )


@pytest.mark.asyncio
async def test_missing_youtube_key_has_actionable_message_in_prefix_and_slash_search(tmp_path):
    bot = create_bot(_settings(tmp_path))
    await bot.database.initialize()
    bot.song_service.search = AsyncMock(return_value=SearchOutcome(
        (), ("YouTube search is not configured (YOUTUBE_API_KEY is empty).",)
    ))
    channel = _Channel(10)
    guild = SimpleNamespace(id=1)
    ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(id=20), channel=channel)
    expected = (
        "⚙️ YouTube search isn't configured yet. The bot owner needs to set "
        "`YOUTUBE_API_KEY` in the `.env` file."
    )

    await bot.get_command("search").callback(ctx, query="Kalyani")

    assert channel.sent == [(expected, {})]

    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(id=20),
        channel=channel,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=followup,
    )
    await bot.tree.get_command("search").callback(interaction, query="Kalyani")

    followup.send.assert_awaited_once_with(expected, ephemeral=True)
    await bot.database.close()


@pytest.mark.asyncio
async def test_youtube_quota_error_is_actionable_and_does_not_expose_provider_details(tmp_path):
    bot = create_bot(_settings(tmp_path, youtube_api_key="configured-key"))
    channel = _Channel(10)

    await bot._send_search(
        guild=SimpleNamespace(id=5), user=SimpleNamespace(id=20), query="Kalyani",
        outcome=SearchOutcome((), ("YouTube API quota is exhausted.",)),
        mode="search", channel=channel,
    )

    reply = channel.sent[0][0]
    assert "API quota" in reply
    assert "ask the bot owner" in reply
    assert "configured-key" not in reply


def test_play_feedback_distinguishes_youtube_load_failure_quota_and_no_results(tmp_path):
    bot = create_bot(_settings(tmp_path))

    unavailable = bot._play_feedback(SearchOutcome((), (
        "YouTube search results were found, but Lavalink could not load playable audio. Try another public result or retry later.",
    )))
    quota = bot._play_feedback(SearchOutcome((), (
        "YouTube API quota is exhausted.",
    )))
    empty = bot._play_feedback(SearchOutcome(()))

    assert "Lavalink could not load playable audio" in unavailable
    assert "API quota" in quota
    assert "Try another title or artist" in empty


@pytest.mark.asyncio
async def test_non_youtube_provider_paths_are_not_registered(tmp_path):
    bot = create_bot(_settings(tmp_path))
    assert set(bot.providers.providers) == {"youtube"}
    assert bot.get_command("play-local") is None
    assert bot.tree.get_command("play-local") is None


@pytest.mark.asyncio
async def test_search_results_are_read_only(tmp_path):
    bot = create_bot(_settings(tmp_path))
    tracks = (
        Track("youtube", "AbCdEfGhI_1", "First Song", "Channel A", 187,
              "https://i.ytimg.com/vi/first/default.jpg",
              "https://www.youtube.com/watch?v=AbCdEfGhI_1"),
        Track("youtube", "ZyXwVuTsRq0", "Second Song", "Channel B", 205,
              "https://i.ytimg.com/vi/second/default.jpg",
              "https://www.youtube.com/watch?v=ZyXwVuTsRq0"),
    )
    channel = _Channel(10)
    guild = SimpleNamespace(id=5)
    user = SimpleNamespace(id=20)

    await bot._send_search(
        guild=guild, user=user, query="Kalyani", outcome=SearchOutcome(tracks),
        mode="search", channel=channel,
    )
    sent_options = channel.sent[0][1]
    assert len(sent_options["embeds"]) == 2
    assert "view" not in sent_options








@pytest.mark.asyncio
async def test_help_documents_auto_play_and_read_only_search(tmp_path):
    bot = create_bot(_settings(tmp_path))
    prefix_context = SimpleNamespace(send=AsyncMock())
    slash_response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(response=slash_response)

    await bot.get_command("help").callback(prefix_context)
    await bot.tree.get_command("help").callback(interaction)

    prefix_embed = prefix_context.send.await_args.kwargs["embed"]
    slash_embed = slash_response.send_message.await_args.kwargs["embed"]
    prefix_text = prefix_embed.description + " " + " ".join(field.value for field in prefix_embed.fields)
    slash_text = slash_embed.description + " " + " ".join(field.value for field in slash_embed.fields)
    assert "!search Kalyani" in prefix_text
    assert "!p Faded" in prefix_text
    assert "!play" in prefix_text
    assert "!select-play" not in prefix_text
    assert "!reselect" not in prefix_text
    assert "!forget-song" not in prefix_text
    assert "!previous" in prefix_text
    assert "/search query: Kalyani" in slash_text
    assert "/play query: Faded" in slash_text
    assert "/select-play" not in slash_text
    assert "/reselect" not in slash_text
    assert "/forget-song" not in slash_text
    assert "/previous" in slash_text
    assert bot.get_command("search") and bot.get_command("p")
    assert bot.tree.get_command("search") and bot.tree.get_command("play")




@pytest.mark.asyncio
async def test_permission_errors_identify_user_vs_bot_permissions(tmp_path):
    bot = create_bot(_settings(tmp_path))
    ctx = SimpleNamespace(send=AsyncMock())

    await bot.on_command_error(ctx, commands.MissingPermissions(["manage_messages"]))
    assert "You lack the required Discord permission (Manage Messages)" in ctx.send.await_args.args[0]

    ctx.send.reset_mock()
    await bot.on_command_error(ctx, commands.BotMissingPermissions(["send_messages"]))
    assert "Miku lacks the required Discord permission (Send Messages)" in ctx.send.await_args.args[0]

    response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock())
    interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))
    await bot.on_app_command_error(interaction, discord.app_commands.MissingPermissions(["manage_messages"]))
    assert "You lack the required Discord permission (Manage Messages)" in response.send_message.await_args.args[0]

    response.send_message.reset_mock()
    await bot.on_app_command_error(interaction, discord.app_commands.BotMissingPermissions(["send_messages"]))
    assert "Miku lacks the required Discord permission (Send Messages)" in response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_unknown_slash_command_points_to_help(tmp_path):
    bot = create_bot(_settings(tmp_path))
    response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock())
    interaction = SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()))

    await bot.on_app_command_error(interaction, discord.app_commands.CommandNotFound("missing", []))

    response.send_message.assert_awaited_once_with(
        "❓ Unknown command. Use `/help` to see Miku's available commands.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_unexpected_errors_are_logged_but_not_shown_to_users(tmp_path, caplog):
    bot = create_bot(_settings(tmp_path))
    ctx = SimpleNamespace(send=AsyncMock())
    secret_sentinel = "private-key-must-not-reach-discord"
    try:
        raise RuntimeError(secret_sentinel)
    except RuntimeError as error:
        prefix_error = commands.CommandInvokeError(error)

    await bot.on_command_error(ctx, prefix_error)
    prefix_reply = ctx.send.await_args.args[0]
    assert "couldn't complete that command" in prefix_reply
    assert secret_sentinel not in prefix_reply
    assert secret_sentinel in caplog.text
    assert "Traceback" in caplog.text

    response = SimpleNamespace(is_done=Mock(return_value=True), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(response=response, followup=followup)
    try:
        raise RuntimeError(secret_sentinel)
    except RuntimeError as error:
        slash_error = error

    await bot.on_app_command_error(interaction, slash_error)

    followup.send.assert_awaited_once()
    slash_reply = followup.send.await_args.args[0]
    assert "couldn't complete that command" in slash_reply
    assert secret_sentinel not in slash_reply
    response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_is_denied_to_non_owner_in_prefix_and_slash_contexts(tmp_path):
    bot = create_bot(_settings(tmp_path, guild_id=1))
    bot.is_owner = AsyncMock(return_value=False)
    command = bot.get_command("sync")
    slash_command = bot.tree.get_command("sync")

    for channel_id in (10, 20):
        ctx = SimpleNamespace(
            bot=bot, author=SimpleNamespace(id=channel_id), channel=_Channel(channel_id), command=None
        )
        with pytest.raises(commands.NotOwner):
            await command.can_run(ctx)

        interaction = SimpleNamespace(user=SimpleNamespace(id=channel_id))
        assert not await slash_command._check_can_run(interaction)


@pytest.mark.asyncio
async def test_startup_sync_targets_configured_development_guild(tmp_path):
    bot = create_bot(_settings(tmp_path, guild_id=1234))
    bot.tree.sync = AsyncMock(return_value=[])
    bot.tree.copy_global_to = Mock()

    await bot.setup_hook()

    guild = discord.Object(id=1234)
    bot.tree.copy_global_to.assert_called_once_with(guild=guild)
    bot.tree.sync.assert_awaited_once_with(guild=guild)
    await bot.database.close()


@pytest.mark.asyncio
async def test_configured_owner_id_controls_owner_checks(tmp_path):
    bot = create_bot(_settings(tmp_path, owner_id=1234))
    assert await bot.is_owner(SimpleNamespace(id=1234)) is True
    assert await bot.is_owner(SimpleNamespace(id=5678)) is False


@pytest.mark.asyncio
async def test_failed_slash_sync_is_logged_without_aborting_startup(tmp_path, caplog):
    bot = create_bot(_settings(tmp_path, guild_id=1234))
    bot.database.initialize = AsyncMock()
    response = SimpleNamespace(status=403, reason="Forbidden")
    bot.sync_application_commands = AsyncMock(
        side_effect=discord.Forbidden(response, {"message": "Missing Access"})
    )

    await bot.setup_hook()

    bot.database.initialize.assert_awaited_once_with()
    assert "Miku will continue online without syncing" in caplog.text




@pytest.mark.asyncio
async def test_auto_play_queues_best_match_without_picker(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    track = Track("youtube", "AbCdEfGhI_1", "Exact Song", "Artist")
    source = PlayableSource("lavalink", object())
    bot.song_service.auto_play_request = AsyncMock(
        return_value=PlayDecision("play_match", track, source=source)
    )
    bot.ensure_voice = AsyncMock()
    bot.playback.enqueue = AsyncMock(return_value=1)
    bot.playback.is_active_track = AsyncMock(return_value=True)
    send_text = AsyncMock()

    await bot._play_text_command(guild, user, "Exact Song", channel, send_text)

    send_text.assert_awaited_once_with("Now playing: **Exact Song**.")
    queued = bot.playback.enqueue.await_args.args[1]
    assert queued.key == track.key
    assert queued.requester == f"User {user.id}"
    bot.playback.enqueue.assert_awaited_once_with(
        guild, queued, channel, resolved_source=source, voice_channel=user.voice.channel,
    )


@pytest.mark.asyncio
async def test_enqueue_during_active_playback_does_not_run_voice_setup(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=56)
    voice_channel = SimpleNamespace(id=7)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=voice_channel))
    track = Track("youtube", "ZyXwVuTsRq0", "Queued Song", "Artist")
    source = PlayableSource("lavalink", object())
    channel = _Channel(9)
    bot.playback.has_active_playback = AsyncMock(return_value=True)
    bot.playback.enqueue = AsyncMock(return_value=2)
    bot.ensure_voice = AsyncMock()

    position = await bot.enqueue_track(
        guild, user, track, channel, resolved_source=source,
    )

    assert position == 2
    bot.ensure_voice.assert_not_awaited()
    bot.playback.enqueue.assert_awaited_once_with(
        guild, Track("youtube", "ZyXwVuTsRq0", "Queued Song", "Artist", requester=f"User {user.id}"),
        channel, resolved_source=source, voice_channel=voice_channel,
    )


@pytest.mark.asyncio
async def test_youtube_play_queues_only_the_resolved_youtube_source(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    query = "Original requested song title"
    track = Track("youtube", "AbCdEfGhI_1", "YouTube match title", "Uploader", 90)
    source = PlayableSource("lavalink", object())
    bot.song_service.auto_play_request = AsyncMock(
        return_value=PlayDecision("play_match", track, source=source)
    )
    bot.enqueue_track = AsyncMock(return_value=1)
    bot.playback.is_active_track = AsyncMock(return_value=False)
    send_text = AsyncMock()

    await bot._play_text_command(guild, user, query, channel, send_text)

    bot.enqueue_track.assert_awaited_once_with(
        guild, user, track, channel, resolved_source=source,
    )


@pytest.mark.asyncio
async def test_slash_play_uses_automatic_matching_without_picker(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    track = Track("youtube", "AbCdEfGhI_1", "Exact Song", "Artist")
    source = PlayableSource("lavalink", object())
    bot.song_service.auto_play_request = AsyncMock(
        return_value=PlayDecision("play_match", track, source=source)
    )
    bot.ensure_voice = AsyncMock()
    bot.playback.enqueue = AsyncMock(return_value=1)
    bot.playback.is_active_track = AsyncMock(return_value=True)
    response = SimpleNamespace(defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild, user=user, channel=channel, response=response, followup=followup
    )

    await bot.tree.get_command("play").callback(interaction, query="Exact Song")

    bot.song_service.auto_play_request.assert_awaited_once_with(guild.id, user.id, "Exact Song")
    queued = bot.playback.enqueue.await_args.args[1]
    assert queued.key == track.key
    assert queued.requester == f"User {user.id}"
    bot.playback.enqueue.assert_awaited_once_with(
        guild, queued, channel, resolved_source=source, voice_channel=user.voice.channel,
    )
    followup.send.assert_awaited_once_with("Now playing: **Exact Song**.", ephemeral=True)


@pytest.mark.asyncio
async def test_slash_play_no_match_responds_after_defer_without_result_cards(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    metadata_only = Track("youtube", "AbCdEfGhI_1", "Faded", "YouTube channel")
    bot.song_service.auto_play_request = AsyncMock(
        return_value=PlayDecision("search", search=SearchOutcome((metadata_only,)))
    )
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild, user=user, channel=channel, response=response, followup=followup,
    )

    await bot.tree.get_command("play").callback(interaction, query="Faded")

    response.defer.assert_awaited_once_with(thinking=True)
    response.send_message.assert_not_awaited()
    followup.send.assert_awaited_once()
    assert "eligible playable match" in followup.send.await_args.args[0]
    assert "embeds" not in followup.send.await_args.kwargs


@pytest.mark.asyncio
async def test_slash_play_resolves_and_queues_the_exact_youtube_video_url(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    query = "https://www.youtube.com/watch?v=AbCdEfGhI_1"
    track = Track("youtube", "AbCdEfGhI_1", "Exact video", "Uploader", 90)
    items = [(track, PlayableSource("lavalink", object()))]
    bot.youtube_provider.resolve_url = AsyncMock(return_value=items)
    bot.enqueue_youtube_items = AsyncMock(return_value=1)
    bot.playback.is_active_track = AsyncMock(return_value=True)
    response = SimpleNamespace(defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild, user=user, channel=channel, response=response, followup=followup
    )

    await bot.tree.get_command("play").callback(interaction, query=query)

    bot.youtube_provider.resolve_url.assert_awaited_once_with(query)
    bot.enqueue_youtube_items.assert_awaited_once_with(guild, user, items, channel)
    assert "Now playing: **Exact video**" in followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_slash_play_queues_direct_youtube_playlist_in_order(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    query = "https://www.youtube.com/playlist?list=PLexample"
    first = Track("youtube", "AbCdEfGhI_1", "First video", "Uploader", 90)
    second = Track("youtube", "ZyXwVuTsRq0", "Second video", "Uploader", 95)
    items = [
        (first, PlayableSource("lavalink", object())),
        (second, PlayableSource("lavalink", object())),
    ]
    bot.youtube_provider.resolve_url = AsyncMock(return_value=items)
    bot.enqueue_youtube_items = AsyncMock(return_value=1)
    bot.playback.is_active_track = AsyncMock(return_value=True)
    response = SimpleNamespace(defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild, user=user, channel=channel, response=response, followup=followup
    )

    await bot.tree.get_command("play").callback(interaction, query=query)

    bot.youtube_provider.resolve_url.assert_awaited_once_with(query)
    bot.enqueue_youtube_items.assert_awaited_once_with(guild, user, items, channel)
    assert "Added **2 tracks** from the YouTube playlist" in followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_slash_play_sends_safe_followup_when_playback_raises_after_defer(tmp_path, caplog):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    track = Track(
        "youtube", "AbCdEfGhI_1", "Exact Video", "Uploader", 90,
        source_url="https://www.youtube.com/watch?v=AbCdEfGhI_1",
    )
    secret_detail = "token=must-not-be-logged; downstream-player-error"
    bot.youtube_provider.resolve_url = AsyncMock(
        return_value=[(track, PlayableSource("lavalink", object()))]
    )
    bot.enqueue_youtube_items = AsyncMock(side_effect=AttributeError(secret_detail))
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild, user=user, channel=channel, response=response, followup=followup
    )

    await bot.tree.get_command("play").callback(
        interaction, query="https://www.youtube.com/watch?v=AbCdEfGhI_1"
    )

    response.defer.assert_awaited_once_with(thinking=True)
    response.send_message.assert_not_awaited()
    followup.send.assert_awaited_once()
    reply = followup.send.await_args.args[0]
    assert "couldn't start playback" in reply
    assert secret_detail not in reply
    assert "downstream-player-error" in caplog.text
    assert "must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_youtube_direct_links_explain_the_lavalink_setup_when_missing(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    bot.providers.resolve_url = AsyncMock()
    send_text = AsyncMock()

    await bot._play_text_command(
        guild, user, "https://www.youtube.com/watch?v=AbCdEfGhI_1", _Channel(9), send_text
    )

    bot.providers.resolve_url.assert_not_awaited()
    assert "LAVALINK_URI" in send_text.await_args.args[0]


@pytest.mark.asyncio
async def test_non_youtube_audio_url_is_rejected_without_provider_resolution(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=55)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=SimpleNamespace(id=7)))
    channel = _Channel(9)
    bot.providers.resolve_url = AsyncMock()
    send_text = AsyncMock()

    await bot._play_text_command(
        guild, user, "https://www.jamendo.com/track/12345/exact", channel, send_text
    )

    bot.providers.resolve_url.assert_not_awaited()
    assert "currently plays YouTube" in send_text.await_args.args[0]


@pytest.mark.asyncio
async def test_ensure_voice_rejects_user_outside_voice_and_missing_bot_permissions(tmp_path):
    bot = create_bot(_settings(tmp_path))
    guild = SimpleNamespace(id=1, voice_client=None, me=object())
    outside = SimpleNamespace(voice=SimpleNamespace(channel=None))
    with pytest.raises(RuntimeError, match="Join a voice channel"):
        await bot.ensure_voice(outside, guild)

    channel = SimpleNamespace(
        id=2,
        permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=False)),
        connect=AsyncMock(),
    )
    member = SimpleNamespace(voice=SimpleNamespace(channel=channel))
    with pytest.raises(RuntimeError, match="Speak permission"):
        await bot.ensure_voice(member, guild)
    channel.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_voice_uses_lavalink_and_does_not_move_active_session(tmp_path, monkeypatch):
    from dataclasses import replace
    import wavelink

    class FakePlayer:
        def __init__(self, guild, channel):
            self.guild = guild
            self.channel = channel
            self.connected = True
            self.playing = False
            self.paused = False

        async def move_to(self, channel):
            self.channel = channel

        async def disconnect(self, **kwargs):
            self.connected = False

    monkeypatch.setattr(wavelink, "Player", FakePlayer)
    settings = replace(
        _settings(tmp_path), lavalink_uri="localhost:2333", lavalink_password="test-password",
    )
    bot = create_bot(settings)
    bot.youtube_playback.ready = True
    guild = SimpleNamespace(id=1, voice_client=None, me=object())
    channel = SimpleNamespace(
        id=2,
        permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=True)),
    )
    async def connect(*, cls):
        voice = cls(guild, channel)
        guild.voice_client = voice
        return voice
    channel.connect = AsyncMock(side_effect=connect)
    member = SimpleNamespace(voice=SimpleNamespace(channel=channel))

    connected = await bot.ensure_voice(member, guild)
    assert isinstance(connected, FakePlayer)
    channel.connect.assert_awaited_once_with(cls=FakePlayer)

    same_channel = await bot.ensure_voice(member, guild)
    assert same_channel is connected
    assert channel.connect.await_count == 1

    other = SimpleNamespace(
        id=3,
        permissions_for=Mock(return_value=SimpleNamespace(connect=True, speak=True)),
        connect=AsyncMock(),
    )
    member.voice.channel = other
    connected.playing = True
    with pytest.raises(RuntimeError, match="different voice channel"):
        await bot.ensure_voice(member, guild)
    assert channel.connect.await_count == 1


@pytest.mark.asyncio
async def test_slash_volume_loop_and_shuffle_use_authorized_playback_controls(tmp_path):
    bot = create_bot(_settings(tmp_path))
    channel = SimpleNamespace(id=9)
    voice_channel = SimpleNamespace(id=7)
    voice = SimpleNamespace(channel=voice_channel, is_connected=Mock(return_value=True))
    guild = SimpleNamespace(id=12, voice_client=voice)
    user = SimpleNamespace(id=77, voice=SimpleNamespace(channel=voice_channel))
    responses = [AsyncMock() for _ in range(3)]
    bot.playback.set_volume = AsyncMock(return_value=65)
    bot.playback.set_loop = AsyncMock(return_value="queue")
    bot.playback.shuffle = AsyncMock(return_value=3)

    await bot.tree.get_command("volume").callback(
        SimpleNamespace(guild=guild, user=user, channel=channel, response=responses[0]),
        value=65,
    )
    await bot.tree.get_command("loop").callback(
        SimpleNamespace(guild=guild, user=user, channel=channel, response=responses[1]),
        mode=discord.app_commands.Choice(name="Queue", value="queue"),
    )
    await bot.tree.get_command("shuffle").callback(
        SimpleNamespace(guild=guild, user=user, channel=channel, response=responses[2]),
    )

    bot.playback.set_volume.assert_awaited_once_with(guild, 65)
    bot.playback.set_loop.assert_awaited_once_with(guild, "queue")
    bot.playback.shuffle.assert_awaited_once_with(guild)
    assert [response.send_message.await_args.args[0] for response in responses] == [
        "Volume set to 65%.",
        "Loop mode set to **queue**.",
        "Shuffled 3 upcoming track(s).",
    ]
