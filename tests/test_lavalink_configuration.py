from pathlib import Path
import re
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = (
    PROJECT_ROOT / "lavalink" / "application.yml",
    PROJECT_ROOT / "lavalink" / "application.yml.example",
)
YOUTUBE_PLUGIN_COMMIT = "34ee4c5f6b4608c19880ff62f80540c0048f537e"
YOUTUBE_PLUGIN_JAR = (
    PROJECT_ROOT / "lavalink" / "plugins" / f"youtube-plugin-{YOUTUBE_PLUGIN_COMMIT}.jar"
)


def _has_line(text: str, pattern: str) -> bool:
    return re.search(pattern, text.replace("\r\n", "\n"), re.MULTILINE) is not None


def test_lavalink_uses_one_local_youtube_plugin_installation():
    assert YOUTUBE_PLUGIN_JAR.is_file()
    with ZipFile(YOUTUBE_PLUGIN_JAR) as plugin:
        names = plugin.namelist()
        assert any(
            name.endswith("dev/lavalink/youtube/plugin/YoutubePluginLoader.class")
            for name in names
        )
        ios_class = next(name for name in names if name.endswith("/clients/Ios.class"))
        ios_bytes = plugin.read(ios_class)
        assert b"21.32.4" in ios_bytes
        assert b"WEB_PLAYER_PARAMS" in ios_bytes
    assert list(YOUTUBE_PLUGIN_JAR.parent.glob("youtube-plugin*.jar")) == [YOUTUBE_PLUGIN_JAR]

    for config_path in CONFIGS:
        config = config_path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*-\s*dependency:\s*\"dev\.lavalink\.youtube:youtube-plugin:",
            config,
            re.MULTILINE,
        )
        assert _has_line(config, r"^      youtube: false$")
        assert _has_line(config, r"^  youtube:\r?\n    enabled: true$")
        assert _has_line(config, r"^    allowSearch: true$")
        assert _has_line(config, r"^    allowDirectVideoIds: true$")
        assert _has_line(config, r"^    allowDirectPlaylistIds: true$")
        for client in ("MUSIC", "ANDROID_VR", "WEB", "WEBEMBEDDED", "IOS"):
            assert _has_line(config, rf"^      - {client}$")

        # YouTube is the only enabled music source; Soundy and legacy sources remain disabled.
        assert _has_line(config, r"^      bandcamp: false$")
        assert _has_line(config, r"^      soundcloud: false$")
        assert _has_line(config, r"^  soundy:\r?\n    enabled: false$")
