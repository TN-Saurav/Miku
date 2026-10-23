from types import SimpleNamespace
from unittest.mock import Mock

import miku.bot as bot_module


def test_main_creates_one_client_and_runs_with_configured_token(monkeypatch):
    settings = SimpleNamespace(
        discord_token="test-token-not-logged", log_level="INFO", guild_id=123,
        voice_commands_enabled=False, prefix_commands_enabled=False, validate=Mock(),
    )
    client = SimpleNamespace(run=Mock())
    create_bot = Mock(return_value=client)
    monkeypatch.setattr(bot_module.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(bot_module, "create_bot", create_bot)

    assert bot_module.main() == 0

    create_bot.assert_called_once_with(settings)
    client.run.assert_called_once_with(settings.discord_token, log_handler=None)
    settings.validate.assert_called_once_with()


def test_main_reports_invalid_configuration_without_creating_client(monkeypatch, caplog):
    settings = SimpleNamespace(
        validate=Mock(side_effect=ValueError("DISCORD_BOT_TOKEN is required.")),
    )
    create_bot = Mock()
    monkeypatch.setattr(bot_module.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(bot_module, "create_bot", create_bot)

    assert bot_module.main() == 2

    assert "DISCORD_BOT_TOKEN is required" in caplog.text
    create_bot.assert_not_called()
