import miku.config as config


def test_from_env_loads_numeric_guild_id_from_project_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_GUILD_ID=954723289907986442\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ENV_FILE", env_file)
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    # Keep the test independent of any environment inherited by the test runner.
    config_keys = (
        "DISCORD_BOT_TOKEN",
        "DISCORD_GUILD_ID",
        "DISCORD_OWNER_ID",
        "PREFIX_COMMANDS_ENABLED",
        "YOUTUBE_API_KEY",
        "JAMENDO_CLIENT_ID",
        "LAVALINK_URI",
        "LAVALINK_PASSWORD",
        "LAVALINK_SECURE",
        "COMMAND_PREFIX",
        "WAKE_WORD",
        "VOICE_COMMANDS_ENABLED",
        "VOICE_CONSENT_REQUIRED",
        "VOICE_LANGUAGE",
        "LOCAL_MUSIC_DIR",
        "DATABASE_PATH",
        "IDLE_DISCONNECT_SECONDS",
        "LOG_LEVEL",
        "VOSK_MODEL_PATH",
        "VOICE_WAKE_CONFIDENCE",
        "VOICE_COMMAND_TIMEOUT_SECONDS",
        "VOICE_SILENCE_TIMEOUT_SECONDS",
        "VOICE_COOLDOWN_SECONDS",
        "VOICE_VAD_RMS_THRESHOLD",
    )
    for key in config_keys:
        monkeypatch.delenv(key, raising=False)

    settings = config.Settings.from_env()

    assert settings.guild_id == 954723289907986442


def test_jamendo_client_id_is_optional_and_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "test-client-id")

    settings = config.Settings.from_env(load_file=False)

    assert settings.jamendo_client_id == "test-client-id"


def test_owner_id_is_loaded_and_must_be_positive(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_OWNER_ID", "123456789012345678")
    settings = config.Settings.from_env(load_file=False)
    settings.validate()
    assert settings.owner_id == 123456789012345678

    monkeypatch.setenv("DISCORD_OWNER_ID", "0")
    invalid = config.Settings.from_env(load_file=False)
    try:
        invalid.validate()
    except ValueError as exc:
        assert "DISCORD_OWNER_ID must be a positive integer" in str(exc)
    else:
        raise AssertionError("non-positive owner ID should be rejected")


def test_lavalink_settings_are_optional_and_normalize_connection_uri(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("LAVALINK_URI", "localhost:2333")
    monkeypatch.setenv("LAVALINK_PASSWORD", "test-password")
    monkeypatch.setenv("LAVALINK_SECURE", "false")

    settings = config.Settings.from_env(load_file=False)
    settings.validate()

    assert settings.lavalink_configured is True
    assert settings.lavalink_connection_uri == "http://localhost:2333"


def test_partial_lavalink_configuration_has_a_clear_error(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("LAVALINK_URI", "localhost:2333")
    monkeypatch.delenv("LAVALINK_PASSWORD", raising=False)

    settings = config.Settings.from_env(load_file=False)

    try:
        settings.validate()
    except ValueError as exc:
        assert "Set both LAVALINK_URI and LAVALINK_PASSWORD" in str(exc)
    else:
        raise AssertionError("partial Lavalink configuration should be rejected")
