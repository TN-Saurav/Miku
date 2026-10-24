from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import main as launcher


def _settings(uri: str = "http://127.0.0.1:2333"):
    return SimpleNamespace(
        lavalink_configured=True,
        lavalink_connection_uri=uri,
        validate=Mock(),
    )


def _skip_bootstrap(monkeypatch):
    monkeypatch.setattr(launcher, "_bootstrap_project_venv", Mock(return_value=None))


def test_launcher_uses_existing_local_lavalink_without_spawning_another(monkeypatch):
    _skip_bootstrap(monkeypatch)
    settings = _settings()
    run_miku = Mock(return_value=0)
    spawn = Mock()
    monkeypatch.setattr(launcher, "_load_settings", Mock(return_value=settings))
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
    _skip_bootstrap(monkeypatch)
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
    monkeypatch.setattr(launcher, "_load_settings", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=False))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value="java"))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "_wait_for_lavalink", Mock())
    monkeypatch.setattr(launcher, "_run_miku", run_miku)
    monkeypatch.setattr(launcher, "_stop_process", stop)

    assert launcher.main() == 0

    spawn.assert_called_once_with(
        ["java", "-jar", str(jar)],
        cwd=lavalink_dir,
        **launcher._process_group_options(),
    )
    run_miku.assert_called_once_with()
    stop.assert_called_once_with(process)


def test_launcher_uses_remote_lavalink_without_starting_local_java(monkeypatch):
    _skip_bootstrap(monkeypatch)
    settings = _settings("https://music.example:2443")
    run_miku = Mock(return_value=0)
    monkeypatch.setattr(launcher, "_load_settings", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_run_miku", run_miku)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        Mock(side_effect=AssertionError("unexpected Java check")),
    )

    assert launcher.main() == 0

    run_miku.assert_called_once_with()


def test_launcher_reports_missing_local_files_without_starting_process(
    monkeypatch, tmp_path, capsys
):
    _skip_bootstrap(monkeypatch)
    settings = _settings()
    lavalink_dir = tmp_path / "lavalink"
    lavalink_dir.mkdir()
    spawn = Mock()
    monkeypatch.setattr(launcher, "LAVALINK_DIR", lavalink_dir)
    monkeypatch.setattr(launcher, "LAVALINK_JAR", lavalink_dir / "Lavalink.jar")
    monkeypatch.setattr(launcher, "LAVALINK_CONFIG", lavalink_dir / "application.yml")
    monkeypatch.setattr(launcher, "_load_settings", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=False))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value="java"))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)

    assert launcher.main() == 2

    assert "Local Lavalink files are missing" in capsys.readouterr().err
    spawn.assert_not_called()


def test_launcher_reports_missing_java_without_starting_process(monkeypatch, capsys):
    _skip_bootstrap(monkeypatch)
    settings = _settings()
    spawn = Mock()
    monkeypatch.setattr(launcher, "_load_settings", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=False))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value=None))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)

    assert launcher.main() == 2

    assert "Java 17 or newer is required" in capsys.readouterr().err
    spawn.assert_not_called()


def test_wait_for_lavalink_detects_open_port(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    port_open = Mock(side_effect=[False, True])
    monkeypatch.setattr(launcher, "_port_is_open", port_open)
    monkeypatch.setattr(launcher.time, "sleep", Mock())

    launcher._wait_for_lavalink("127.0.0.1", 2333, process, timeout=1)

    assert port_open.call_count == 2


def test_launcher_stops_its_lavalink_child_on_ctrl_c(monkeypatch, tmp_path, capsys):
    _skip_bootstrap(monkeypatch)
    settings = _settings()
    lavalink_dir = tmp_path / "lavalink"
    lavalink_dir.mkdir()
    jar = lavalink_dir / "Lavalink.jar"
    config = lavalink_dir / "application.yml"
    jar.touch()
    config.touch()
    process = Mock()
    process.poll.return_value = None
    stop = Mock()
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "LAVALINK_DIR", lavalink_dir)
    monkeypatch.setattr(launcher, "LAVALINK_JAR", jar)
    monkeypatch.setattr(launcher, "LAVALINK_CONFIG", config)
    monkeypatch.setattr(launcher, "_load_settings", Mock(return_value=settings))
    monkeypatch.setattr(launcher, "_port_is_open", Mock(return_value=False))
    monkeypatch.setattr(launcher.shutil, "which", Mock(return_value="java"))
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(launcher, "_wait_for_lavalink", Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(launcher, "_stop_process", stop)

    assert launcher.main() == 130

    stop.assert_called_once_with(process)
    assert "launcher stopped" in capsys.readouterr().out.casefold()


def test_run_miku_stops_child_on_ctrl_c(monkeypatch, tmp_path, capsys):
    process = Mock()
    process.wait.side_effect = KeyboardInterrupt
    stop = Mock()
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher.sys, "executable", "project-python")
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(launcher, "_stop_process", stop)

    assert launcher._run_miku() == 130

    stop.assert_called_once_with(process)
    assert "Miku stopped by keyboard interrupt" in capsys.readouterr().out


def test_stop_process_signals_child_and_waits_for_exit():
    process = Mock()
    process.poll.return_value = None

    launcher._stop_process(process)

    if launcher.os.name == "nt":
        process.send_signal.assert_called_once_with(launcher.signal.CTRL_BREAK_EVENT)
        process.terminate.assert_not_called()
    else:
        process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)


def test_stop_process_kills_child_if_graceful_shutdown_times_out():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [
        launcher.subprocess.TimeoutExpired(cmd="child", timeout=10),
        None,
    ]

    launcher._stop_process(process)

    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2


def test_bootstrap_restarts_with_project_venv_and_forwards_arguments(monkeypatch, tmp_path):
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    python = venv_dir / "Scripts" / "python.exe"
    python.parent.mkdir()
    python.touch()
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(launcher, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launcher, "VENV_PYTHON", python)
    monkeypatch.setattr(launcher.sys, "prefix", str(tmp_path / "global-python"))
    monkeypatch.setattr(launcher.sys, "argv", ["main.py", "--check"])
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher.subprocess, "run", run)

    assert launcher._bootstrap_project_venv() == 0

    run.assert_called_once_with(
        [str(python), str(tmp_path / "main.py"), "--check"],
        cwd=tmp_path,
        check=False,
    )


def test_bootstrap_does_not_relaunch_when_already_in_project_venv(monkeypatch, tmp_path):
    venv_dir = tmp_path / ".venv"
    monkeypatch.setattr(launcher, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launcher.sys, "prefix", str(venv_dir))
    run = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)

    assert launcher._bootstrap_project_venv() is None

    run.assert_not_called()


def test_bootstrap_reports_missing_venv_without_starting_process(
    monkeypatch, tmp_path, capsys
):
    venv_dir = tmp_path / ".venv"
    monkeypatch.setattr(launcher, "VENV_DIR", venv_dir)
    monkeypatch.setattr(launcher, "VENV_PYTHON", venv_dir / "Scripts" / "python.exe")
    monkeypatch.setattr(launcher.sys, "prefix", str(tmp_path / "global-python"))
    run = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)

    assert launcher._bootstrap_project_venv() == 2

    assert "Project virtual environment is missing" in capsys.readouterr().err
    run.assert_not_called()


def test_start_bat_calls_the_project_venv_and_shared_launcher():
    batch = (launcher.PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    assert 'set "MIKU_PYTHON=%~dp0.venv\\Scripts\\python.exe"' in batch
    assert '"%MIKU_PYTHON%" "%~dp0main.py" %*' in batch
    assert "java -jar" not in batch
    assert "pause" in batch.casefold()
