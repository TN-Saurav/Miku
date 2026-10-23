# Miku Discord Music Bot

Miku is a Discord bot for slash commands, optional `!` prefix commands, YouTube audio playback through Lavalink, and an opt-in voice-command pipeline. It does not act as a conversational assistant.

## What works

- YouTube is the only music provider exposed by search and playback. Lavalink is the playback engine; Miku does not switch to Jamendo, local files, SoundCloud, or Bandcamp.
- `/play` and the primary prefix command `!p` automatically play the best-ranked playable search result. `!play` remains an alias. `/search` and `!search` show read-only metadata results; they do not present selection controls or save preferences.
- YouTube Data API supplies metadata when configured. Public YouTube video and playlist audio is resolved through an authorized Lavalink v4 server with the maintained `youtube-source` plugin. Miku does not use YouTube cookies, account tokens, or restricted-video bypasses. Private, deleted, age-restricted, region-restricted, live, or otherwise unsupported content may fail; failed tracks are marked Failed and the queue advances.
- Playback has a per-guild queue and a separate interactive player message for each track. Pause, resume, skip, previous, queue, loop, volume, and stop controls use Lavalink. Previous replays the last completed track.
- Voice commands are off by default. After explicit consent, only the consenting speaker's short frames are processed locally by a Vosk wake-word recognizer and command recognizer. No recording or transcript is written to disk.

The optional `discord-ext-voice-recv` dependency is alpha software; its maintainer notes that it is incomplete and does not guarantee stability. The bot keeps voice receiving isolated so a voice dependency or model problem does not disable text commands. Hindi/Hinglish recognition depends on the installed Vosk model and audio conditions; it is not perfect. Voice receiving has not been exercised against a live Discord server in this workspace.

## Windows 10/11 setup (PowerShell)

### 1. Install Python and FFmpeg

Install Python 3.11 or 3.12 from [python.org](https://www.python.org/downloads/windows/) and select **Add Python to PATH**. This code also supports the Python 3.10 runtime used for its local test run.

Install FFmpeg from PowerShell:

```powershell
winget install --id Gyan.FFmpeg --exact
```

This WinGet package is declared GPL-3.0. If you distribute a bundled FFmpeg build, choose and review a build whose enabled libraries and license fit your distribution; FFmpeg build licenses depend on the included libraries.

Close and reopen PowerShell, then confirm both tools:

```powershell
py --version
ffmpeg -version
```

### 2. Create a Discord bot

1. In the Discord Developer Portal, create an application and add a bot.
2. Copy the **bot token** from the bot page into your local `.env`. Do not use a Discord user-account token.
3. Under **OAuth2 → URL Generator**, select `bot` and `applications.commands`.
4. Grant the bot only the permissions it needs: View Channels, Send Messages, Embed Links, Read Message History, Connect, and Speak. Invite it to your server with the generated URL.
5. Slash commands do not need the **Message Content Intent**. Prefix commands are optional; to use them, set `PREFIX_COMMANDS_ENABLED=true` in `.env` and enable Message Content Intent on the bot page. Prefix commands are off by default so Miku can start without a privileged intent.
6. Copy your server ID with Discord Developer Mode enabled.

### 3. Install the bot

From this project directory:

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `DISCORD_BOT_TOKEN`. `DISCORD_GUILD_ID` is optional; setting it makes slash-command sync immediate for that server during development. Without it, commands sync globally and may take some time to appear. Prefix command responses and playback notices use the channel where the command was invoked. Slash commands are available throughout the guild, subject to Discord's channel permissions. Voice-triggered notices use the guild's system channel when one is configured.

For richer YouTube metadata, set `YOUTUBE_API_KEY` to an API key for a Google Cloud project with YouTube Data API v3 enabled. Search uses `search.list` and a second `videos.list` lookup for duration/availability; YouTube quota can be exhausted. Without a key, Miku searches YouTube through Lavalink.

### Lavalink YouTube audio setup

YouTube audio requires a Lavalink v4 server that you own or are authorized to use. Wavelink 3.5.2 connects the bot to Lavalink; installing Wavelink alone does not provide a YouTube source. The server must load the maintained Lavalink YouTube plugin (`youtube-source`). Lavalink requires Java 17 or newer. Use only content and service access that you are authorized to play; Miku does not use cookies, account credentials, OAuth refresh tokens, proof tokens, or access-control workarounds. The audio source may reject videos that are private, deleted, age-restricted, region-restricted, live, or otherwise unsupported.

From the project root, download Lavalink and prepare its local configuration:

```powershell
New-Item -ItemType Directory -Force .\lavalink
Invoke-WebRequest -Uri "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar" -OutFile .\lavalink\Lavalink.jar
Copy-Item .\lavalink\application.yml.example .\lavalink\application.yml
notepad .\lavalink\application.yml
```

The example binds Lavalink to `127.0.0.1`, disables built-in non-YouTube sources, and configures the official YouTube plugin snapshot from commit `34ee4c5f6b4608c19880ff62f80540c0048f537e`. The JAR is installed once at `lavalink/plugins/youtube-plugin-34ee4c5f6b4608c19880ff62f80540c0048f537e.jar`, with no duplicate Maven declaration. The configuration enables the documented `IOS` playback client. Soundy remains disabled. In `application.yml`, replace `CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD` with a private password. Keep the file local; do not publish it. The example uses a local-only bind so the node is not exposed to the internet.

Install Java 17 or newer. Start Lavalink in a separate PowerShell window:

```powershell
Set-Location "C:\Users\saura\OneDrive\Desktop\miku\lavalink"
java -jar .\Lavalink.jar
```

In the project's existing `.env`, set `LAVALINK_URI=127.0.0.1:2333`, set `LAVALINK_PASSWORD` to the same password as the Lavalink config, and set `LAVALINK_SECURE=false`. If your authorized server uses TLS, use an `https://` URI and `LAVALINK_SECURE=true`. Keep the Lavalink terminal running while Miku is online. Restart Lavalink after changing `application.yml`. Miku can search YouTube through Lavalink when `YOUTUBE_API_KEY` is blank; the Google API key adds official metadata and its quota limits.

### 4. Run and check

From the project root in PowerShell:

```powershell
Set-Location "C:\Users\saura\OneDrive\Desktop\miku"
.\.venv\Scripts\python.exe -m miku
```

The equivalent package-module command `python -m miku.bot` also works. Keep PowerShell in the project root so `.env` and relative data paths resolve as expected. Miku validates `DISCORD_BOT_TOKEN` before connecting, logs startup and connection status, then stays running in the foreground until stopped with Ctrl+C. The bot syncs slash commands on startup. Keep the Lavalink terminal open for YouTube playback. Join a voice channel and try `/play query: https://www.youtube.com/watch?v=VIDEO_ID` for the exact video's audio. A YouTube playlist URL queues the tracks Lavalink can load. For song-name queries, Miku checks ranked results and selects the first one Lavalink can load. `/search` shows read-only YouTube results.

For prefix commands, set `PREFIX_COMMANDS_ENABLED=true` in `.env`, enable Message Content Intent in the Developer Portal, then use `!p` for playback (`!play` is an alias), `!search`, `!queue`, `!pause`, `!resume`, `!skip`, and the other documented playback commands.

### 6. Optional voice commands

Voice commands require a Vosk model installed locally and the optional receiver dependencies. To prepare a Hindi model, download an appropriate model from the [Vosk model list](https://alphacephei.com/vosk/models) and extract it to a private local directory. Set `VOSK_MODEL_PATH` to that extracted directory. The chosen model must support the configured `VOICE_LANGUAGE`; the Hindi model supports Hindi but code-switching and English song titles can be misrecognized.

In the virtual environment:

```powershell
python -m pip install -r requirements-voice.txt
```

Set `VOICE_COMMANDS_ENABLED=true`, keep `VOICE_CONSENT_REQUIRED=true`, and configure `VOSK_MODEL_PATH`. Restart Miku. Run `/voice-status`; then `/voice-listening enable` and press the consent button. Miku posts a visible notice before starting to receive audio. Say a supported command beginning with “Miku”, such as “Miku, pause” or “Miku, search Kalyani”. `/voice-listening disable` immediately stops receiving. If listening is active, only the user who enabled it is processed; other speakers are ignored. `VOICE_COMMAND_TIMEOUT_SECONDS`, `VOICE_SILENCE_TIMEOUT_SECONDS`, `VOICE_COOLDOWN_SECONDS`, `VOICE_WAKE_CONFIDENCE`, and `VOICE_VAD_RMS_THRESHOLD` tune the short capture and wake gate.

## Commands

| Command | Purpose |
|---|---|
| `/join`, `/leave` | Join your voice channel; stop and disconnect |
| `/search query` | Search YouTube metadata |
| `/play query` | Automatically play the best-ranked YouTube result Lavalink can load; direct YouTube URLs play the exact linked video |
| `/pause`, `/resume`, `/skip`, `/previous`, `/stop` | Control playback; users must be in Miku's active voice channel |
| `/queue`, `/nowplaying`, `/remove position`, `/clear` | Inspect and edit the per-guild queue |
| `/volume value` | Set playback volume from 0 to 100 |
| `/loop mode` | Set loop mode to `off`, `track`, or `queue` |
| `/shuffle` | Shuffle upcoming tracks |
| `/voice-status` | Check voice-listening readiness and status |
| `/voice-listening enable`, `/voice-listening disable` | Consent to and stop voice receiving |
| `/help` | Show command summary |
| `/sync`, `!sync` | Re-sync slash commands; bot owner only |

Common playback, search, memory, and voice status commands have `!` prefix equivalents. Voice equivalents include “Miku, play Kalyani”, “Miku, search Kalyani”, “Miku, change Kalyani”, “Miku, play the second result”, “Miku, pause”, “Miku, resume”, “Miku, skip”, “Miku, queue”, and “Miku, leave”. Unsupported phrases and ordinary conversation are ignored.

## Configuration

Prefix equivalents are available only when `PREFIX_COMMANDS_ENABLED=true` and Message Content Intent is enabled in the Developer Portal.

See `.env.example` for all supported variables. Secrets remain in `.env`; the bot never saves credentials in SQLite or logs.

| Variable | Meaning |
|---|---|
| `DISCORD_BOT_TOKEN` | Required Discord bot token. Never use a user-account token. |
| `DISCORD_GUILD_ID` | Optional development server for immediate slash-command sync. |
| `DISCORD_OWNER_ID` | Optional numeric Discord user ID for owner-only commands such as `/sync`; when blank, Discord application ownership is used. |
| `PREFIX_COMMANDS_ENABLED` | Enable `!` commands and request Message Content Intent; default `false`. Also enable that intent in the Developer Portal. |
| `YOUTUBE_API_KEY` | Optional official YouTube Data API key for richer metadata; Lavalink search works without it when configured. |
| `LAVALINK_URI` | Optional Lavalink v4 node address, such as `127.0.0.1:2333` or an `https://` endpoint. |
| `LAVALINK_PASSWORD` | Password matching the authorized Lavalink server config. Required with `LAVALINK_URI`. |
| `LAVALINK_SECURE` | Use TLS for a bare host address; default `false`. A URI with `https://` is used as supplied. |
| `COMMAND_PREFIX` | Prefix for optional message commands; default `!`. |
| `WAKE_WORD` | Voice wake word; default `miku`. |
| `VOICE_COMMANDS_ENABLED` | Master voice-feature switch; default `false`. |
| `VOICE_CONSENT_REQUIRED` | Default `true`; this build always requires the consent notice/button even if set to `false`. |
| `VOICE_LANGUAGE` | Expected model language tag; default `hi-IN`. The model itself is selected by `VOSK_MODEL_PATH`. |
| `VOSK_MODEL_PATH` | Local folder containing the installed Vosk model. Empty disables voice startup. |
| `VOICE_WAKE_CONFIDENCE` | Wake-word confidence threshold from `0` to `1`; default `0.75`. |
| `VOICE_COMMAND_TIMEOUT_SECONDS` | Maximum short capture window from `2` to `10` seconds; default `5`. |
| `VOICE_SILENCE_TIMEOUT_SECONDS` | Silence needed to finish a command from `0.2` to `2` seconds; default `0.6`. |
| `VOICE_COOLDOWN_SECONDS` | Minimum delay between wake-word triggers from `0.5` to `10` seconds; default `2`. |
| `VOICE_VAD_RMS_THRESHOLD` | Local audio energy threshold for voice activity; default `250`. |
| `DATABASE_PATH` | SQLite file path; default `data/miku.sqlite3`. |
| `IDLE_DISCONNECT_SECONDS` | Optional idle disconnect delay; `0` disables it. Listening mode stays connected. |
| `LOG_LEVEL` | Python logging level; default `INFO`. |

## Tests and limitations

The automated tests mock Discord, Lavalink, and YouTube search/load calls. They cover exact YouTube URL and playlist resolution, fallback between ranked YouTube results when audio cannot load, Lavalink queue transitions and event deduplication, failure statuses, voice reconnect behavior, secret-safe errors, voice command parsing, multi-channel commands, owner-only sync, and guild slash-command sync. They do not prove operation with real Discord credentials, a live Lavalink node and YouTube source, Google quota, FFmpeg on another machine, voice-receive compatibility, or a particular speech model. Playback queues live in memory and are cleared if Miku restarts; unexpected voice disconnects mark the current track failed and preserve upcoming tracks for reconnection.

Troubleshooting:

- **FFmpeg not found:** reopen PowerShell after installation, check `ffmpeg -version`, and ensure `ffmpeg.exe` is on `PATH`.
- **Cannot connect or speak:** check the bot's Connect/Speak permissions and that it is not being moved to a disallowed channel.
- **Commands missing:** check `applications.commands`, confirm the bot has access to the `DISCORD_GUILD_ID` server, and restart Miku. A failed sync is logged while the bot remains online.
- **Prefix commands unavailable:** set `PREFIX_COMMANDS_ENABLED=true` and enable Message Content Intent in the Developer Portal. Slash commands work without that privileged intent.
- **YouTube returns no results:** check the API key, YouTube Data API v3 enablement, and quota. No key is printed in logs or embeds.
- **YouTube playback says Lavalink is not configured:** start your authorized Lavalink v4 server with the `youtube-source` plugin, then set matching `LAVALINK_URI` and `LAVALINK_PASSWORD` values in `.env`.
- **Lavalink is configured but YouTube playback fails:** check the Lavalink terminal for YouTube plugin load errors and the specific video-load error. Miku marks the failed track and advances to the next YouTube request; it does not switch music providers or bypass YouTube restrictions. Confirm the YouTube plugin is enabled and legacy sources are disabled, then restart Lavalink.
- **Old player controls after a restart:** playback queues are in memory and do not resume. Start a new `/play` request; stale controls respond with a restart message.
- **Voice dependency fails:** text commands remain available. Check Python version, `requirements-voice.txt`, and `/voice-status`; voice receiving is an alpha extension.
- **Speech is inaccurate:** confirm the model language matches `VOICE_LANGUAGE`, use a quiet voice channel, speak the wake word clearly, and adjust `VOICE_WAKE_CONFIDENCE` only with care.

## Dependency and licensing notes

Pinned runtime packages include `discord.py` (MIT), `wavelink` (MIT), `aiohttp` (Apache-2.0 and MIT components), `aiosqlite` (MIT), `python-dotenv` (BSD-3-Clause), and `tinytag` (MIT). Pytest packages are development-only. Lavalink and `youtube-source` are separate Java components with their own licenses and current usage constraints. The optional receiver is MIT-licensed alpha software; Vosk and its model are Apache-2.0 according to their project pages. Review each package, server, plugin, model, and service's terms before distribution. FFmpeg has separate LGPL/GPL build variants, so choose a build whose license matches your distribution needs. YouTube Data API use is governed by Google's API terms and quota rules.
