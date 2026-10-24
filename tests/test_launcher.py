from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import main as launcher


def _settings(uri: str = "http://127.0.0.1:2333"):
    return SimpleNamespace(
        lavalink_configured=True,
        lavalink_connection_uri=uri,
        validate=Mock(),
    )


def test_launcher_uses_existing_local_lavalink_without_spawning_another(monkeypatch):
    settings = _settings()
    run_miku = Mock(return_value=0)
    spawn = Mock()
    monkeypatch.setattr(launcher.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=True))
    monkeypatch.setattr(launcher, "_run_miku", run_miku)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)

    assert launcher.main() == 0

    settings.validate.assert_called_once_with()
    run_miku.assert_called_once_with()
    spawn.assert_not_called()


def test_launcher_starts_and_stops_local_lavalink_then_runs_package_entrypoint(
    monkeypatch, tmp_path
):
    settings = _settings()
    lavalink_dir = tmp_path / "lavalink"
    lavalink_dir.mkdir()
    jar = lavalink_dir / "Lavalink.jar"
    config = lavalink_dir / "application.yml"
    jar.touch()
    config.touch()
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    run_miku = Mock(return_value=0)
    stop = Mock()
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "LAVALINK_DIR", lavalink_dir)
    monkeypatch.setattr(launcher, "LAVALINK_JAR", jar)
    monkeypatch.setattr(launcher, "LAVALINK_CONFIG", config)
    monkeypatch.setattr(launcher.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=False))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value="java"))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "_wait_for_lavalink", Mock())
    monkeypatch.setattr(launcher, "_run_miku", run_miku)
    monkeypatch.setattr(launcher, "_stop_lavalink", stop)

    assert launcher.main() == 0

    spawn.assert_called_once_with(["java", "-jar", str(jar)], cwd=lavalink_dir)
    run_miku.assert_called_once_with()
    stop.assert_called_once_with(process)


def test_launcher_uses_remote_lavalink_without_starting_local_java(monkeypatch):
    settings = _settings("https://music.example:2443")
    run_miku = Mock(return_value=0)
    monkeypatch.setattr(launcher.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_run_miku", run_miku)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        Mock(side_effect=AssertionError("unexpected Java check")),
    )

    assert launcher.main() == 0

    settings.validate.assert_called_once_with()
    run_miku.assert_called_once_with()


def test_launcher_reports_missing_local_files_without_starting_process(monkeypatch, tmp_path, capsys):
    settings = _settings()
    lavalink_dir = tmp_path / "lavalink"
    lavalink_dir.mkdir()
    spawn = Mock()
    monkeypatch.setattr(launcher, "LAVALINK_DIR", lavalink_dir)
    monkeypatch.setattr(launcher, "LAVALINK_JAR", lavalink_dir / "Lavalink.jar")
    monkeypatch.setattr(launcher, "LAVALINK_CONFIG", lavalink_dir / "application.yml")
    monkeypatch.setattr(
        launcher.Settings,
        "from_env",
        Mock(return_value=settings),
    )
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=False))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value="java"))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)

    assert launcher.main() == 2

    assert "Local Lavalink files are missing" in capsys.readouterr().err
    spawn.assert_not_called()


def test_wait_for_lavalink_detects_open_port(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    port_open = Mock(side_effect=[False, True])
    monkeypatch.setattr(launcher, "_port_is_open", port_open)
    monkeypatch.setattr(launcher.time, "sleep", Mock())

    launcher._wait_for_lavalink("127.0.0.1", 2333, process, timeout=1)

    assert port_open.call_count == 2
