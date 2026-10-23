from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


def _float(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    guild_id: int | None
    prefix_commands_enabled: bool
    youtube_api_key: str
    command_prefix: str
    wake_word: str
    voice_commands_enabled: bool
    voice_consent_required: bool
    voice_language: str
    local_music_dir: Path
    database_path: Path
    idle_disconnect_seconds: int
    log_level: str
    vosk_model_path: Path | None
    voice_wake_confidence: float
    voice_command_timeout_seconds: int
    voice_silence_timeout_seconds: float
    voice_cooldown_seconds: float
    voice_vad_rms_threshold: float
    jamendo_client_id: str = ""
    lavalink_uri: str = ""
    lavalink_password: str = ""
    lavalink_secure: bool = False
    owner_id: int | None = None

    @property
    def lavalink_configured(self) -> bool:
        return bool(self.lavalink_uri and self.lavalink_password)

    @property
    def lavalink_connection_uri(self) -> str:
        if not self.lavalink_uri:
            return ""
        if "://" in self.lavalink_uri:
            return self.lavalink_uri
        scheme = "https" if self.lavalink_secure else "http"
        return f"{scheme}://{self.lavalink_uri}"

    def validate(self) -> None:
        if not self.discord_token:
            raise ValueError(
                "DISCORD_BOT_TOKEN is required. Set it in the project's .env file before starting Miku."
            )
        if self.guild_id is not None and self.guild_id <= 0:
            raise ValueError("DISCORD_GUILD_ID must be a positive integer when set.")
        if self.owner_id is not None and self.owner_id <= 0:
            raise ValueError("DISCORD_OWNER_ID must be a positive integer when set.")
        if bool(self.lavalink_uri) != bool(self.lavalink_password):
            raise ValueError(
                "Set both LAVALINK_URI and LAVALINK_PASSWORD to enable YouTube audio playback."
            )
        if self.lavalink_uri:
            from urllib.parse import urlparse

            parsed = urlparse(self.lavalink_connection_uri)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment):
                raise ValueError(
                    "LAVALINK_URI must be a host and port or an http(s) URL without credentials."
                )
        if self.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError(
                "LOG_LEVEL must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG, or NOTSET."
            )

    @classmethod
    def from_env(cls, *, load_file: bool = True) -> "Settings":
        if load_file:
            load_dotenv(dotenv_path=PROJECT_ENV_FILE)
        model_path = os.getenv("VOSK_MODEL_PATH", "").strip()
        return cls(
            discord_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
            guild_id=_optional_int("DISCORD_GUILD_ID"),
            prefix_commands_enabled=_bool("PREFIX_COMMANDS_ENABLED", False),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY", "").strip(),
            command_prefix=os.getenv("COMMAND_PREFIX", "!").strip() or "!",
            wake_word=os.getenv("WAKE_WORD", "miku").strip().casefold() or "miku",
            voice_commands_enabled=_bool("VOICE_COMMANDS_ENABLED", False),
            voice_consent_required=_bool("VOICE_CONSENT_REQUIRED", True),
            voice_language=os.getenv("VOICE_LANGUAGE", "hi-IN").strip(),
            local_music_dir=Path(os.getenv("LOCAL_MUSIC_DIR", "music")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/miku.sqlite3")),
            idle_disconnect_seconds=max(0, _int("IDLE_DISCONNECT_SECONDS", 0)),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            vosk_model_path=Path(model_path) if model_path else None,
            voice_wake_confidence=min(1.0, max(0.0, _float("VOICE_WAKE_CONFIDENCE", 0.75))),
            voice_command_timeout_seconds=max(2, min(10, _int("VOICE_COMMAND_TIMEOUT_SECONDS", 5))),
            voice_silence_timeout_seconds=max(0.2, min(2.0, _float("VOICE_SILENCE_TIMEOUT_SECONDS", 0.6))),
            voice_cooldown_seconds=max(0.5, min(10.0, _float("VOICE_COOLDOWN_SECONDS", 2.0))),
            voice_vad_rms_threshold=max(0.0, _float("VOICE_VAD_RMS_THRESHOLD", 250.0)),
            jamendo_client_id=os.getenv("JAMENDO_CLIENT_ID", "").strip(),
            lavalink_uri=os.getenv("LAVALINK_URI", "").strip(),
            lavalink_password=os.getenv("LAVALINK_PASSWORD", "").strip(),
            lavalink_secure=_bool("LAVALINK_SECURE", False),
            owner_id=_optional_int("DISCORD_OWNER_ID"),
        )
