from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = PROJECT_ROOT / "scripts" / "setup-windows.ps1"
SCRIPT_TEXT = SETUP_SCRIPT.read_text(encoding="utf-8")


def test_windows_setup_pins_verified_official_artifacts():
    assert '$LavalinkVersion = "4.2.2"' in SCRIPT_TEXT
    assert (
        '$LavalinkUrl = "https://github.com/lavalink-devs/Lavalink/releases/download/'
        '4.2.2/Lavalink.jar"'
    ) in SCRIPT_TEXT
    assert '$YouTubePluginVersion = "1.18.2"' in SCRIPT_TEXT
    assert (
        '$YouTubePluginUrl = "https://maven.lavalink.dev/releases/dev/lavalink/youtube/'
        'youtube-plugin/1.18.2/youtube-plugin-1.18.2.jar"'
    ) in SCRIPT_TEXT
    for name in ("LavalinkSha256", "YouTubePluginSha256"):
        match = re.search(rf'\${name} = "([0-9a-f]+)"', SCRIPT_TEXT)
        assert match and len(match.group(1)) == 64


def test_setup_checks_java_and_validates_downloads_before_installing():
    assert "Java 17 or newer is required" in SCRIPT_TEXT
    assert "$major -lt 17" in SCRIPT_TEXT
    assert "Get-FileHash" in SCRIPT_TEXT
    assert "ZipFile]::OpenRead" in SCRIPT_TEXT
    assert "YoutubePluginLoader.class" in SCRIPT_TEXT
    assert "Implementation-Version" in SCRIPT_TEXT
    assert "[System.IO.File]::Move($temporaryPath, $Destination)" in SCRIPT_TEXT
    assert "^lavalink:\\s*(?:#.*)?$" in SCRIPT_TEXT
    assert "lavalink.server.password" in SCRIPT_TEXT


def test_setup_preserves_existing_configs_and_avoids_duplicate_plugins():
    copy_function = SCRIPT_TEXT.split("function Copy-TemplateIfMissing", 1)[1].split(
        "function Get-DotEnvValue", 1
    )[0]
    assert "if (Test-Path -LiteralPath $Destination)" in copy_function
    assert "Existing $Label was preserved." in copy_function
    assert "Copy-Item -LiteralPath $Template -Destination $Destination" in copy_function
    assert "Copy-Item -LiteralPath $Template -Destination $Destination -Force" not in SCRIPT_TEXT
    assert '"youtube-plugin*.jar"' in SCRIPT_TEXT
    assert "no second plugin was downloaded" in SCRIPT_TEXT


def test_gitignore_protects_secrets_artifacts_and_runtime_data():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".env.*", "!.env.example", "*.jar", ".venv/", "__pycache__/",
        ".pytest_cache/", "*.egg-info/", "logs/", "data/", "lavalink/application.yml",
    ):
        assert pattern in ignore


def test_env_and_lavalink_examples_share_only_a_placeholder_password():
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    server_example = (PROJECT_ROOT / "lavalink" / "application.yml.example").read_text(encoding="utf-8")
    keys = [
        line.split("=", 1)[0]
        for line in env_example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    assert len(keys) == len(set(keys))
    env_password = re.search(r"(?m)^LAVALINK_PASSWORD=(.+)$", env_example)
    server_password = re.search(r"(?m)^\s+password:\s*(.+)$", server_example)
    assert env_password and server_password
    configured_server_password = server_password.group(1).strip().strip("\"'")
    assert env_password.group(1) == configured_server_password == "CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD"
    assert re.search(r"(?m)^DISCORD_BOT_TOKEN=$", env_example)
    assert re.search(r"(?m)^YOUTUBE_API_KEY=$", env_example)
    assert "JAMENDO_CLIENT_ID" not in env_example


def test_readme_uses_non_overwriting_windows_setup_steps():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert ".\\scripts\\setup-windows.ps1" in readme
    assert "Set-ExecutionPolicy -Scope Process Bypass" in readme
    assert "Copy-Item .env.example .env" not in readme
    assert "C:\\Users\\saura" not in readme


def test_runtime_and_optional_dependencies_are_separated():
    runtime_requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "pytest" not in runtime_requirements
    assert 'dev = [\n    "pytest==8.4.1",' in pyproject
    assert 'voice = [\n    "discord-ext-voice-recv==' in pyproject
    assert 'miku = "miku.bot:main"' in pyproject
    assert "python -m miku" in (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
