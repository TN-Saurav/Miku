from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_EXAMPLE = PROJECT_ROOT / "lavalink" / "application.yml.example"


def _has_line(text: str, pattern: str) -> bool:
    return re.search(pattern, text.replace("\r\n", "\n"), re.MULTILINE) is not None


def test_lavalink_example_enables_youtube_and_disables_other_sources():
    config = CONFIG_EXAMPLE.read_text(encoding="utf-8")
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
