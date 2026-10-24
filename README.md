# Miku Discord Music Bot

Miku is a Discord bot with slash commands, optional prefix commands, YouTube playback through Lavalink, and optional local voice commands. YouTube is the only music provider exposed by Miku's search and playback commands. Playback queues are in memory and clear when Miku restarts.

## Prerequisites

- Python 3.10 or newer (Python 3.11 or 3.12 recommended on Windows).
- A Discord application with a bot and the `applications.commands` scope.
- An authorized Lavalink v4 server with the `youtube-source` plugin for audio playback. If you run Lavalink locally, install Java 17 or newer; Java 21 is recommended.

## Windows setup (PowerShell)

Run these commands from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup-windows.ps1
```

The setup script checks Java, downloads the pinned Lavalink and YouTube plugin artifacts from official project repositories, and verifies their SHA-256 hashes. It creates `.env` and `lavalink/application.yml` from the example files only when they are missing. Existing local files and JARs are left in place. Review both generated configuration files before starting anything.

In `.env`, set `DISCORD_BOT_TOKEN`. For local Lavalink, replace the example password in both `.env` (`LAVALINK_PASSWORD`) and `lavalink/application.yml` (`lavalink.server.password`) with the same private value. Never commit either local file. The examples contain placeholders; Miku will reject an empty bot token before attempting to log in.

Invite the bot with the `bot` and `applications.commands` scopes and the permissions it needs: View Channels, Send Messages, Embed Links, Read Message History, Connect, and Speak. Prefix commands are optional. Enable Message Content Intent in the Discord Developer Portal and set `PREFIX_COMMANDS_ENABLED=true` in `.env` to use them.

### Starting Lavalink and Miku

From the repository root, start both with one command:

```powershell
python .\main.py
```

`main.py` re-launches itself with `.venv`'s Python when needed, starts the local Lavalink JAR when `LAVALINK_URI` points to localhost and its port is closed, waits for the port, then delegates to the existing `python -m miku` entry point. It reuses an open local port and stops only a Lavalink process it started. A remote `LAVALINK_URI` starts Miku without launching a local server. Press Ctrl+C to stop Miku and any Lavalink process started by this launcher. PowerShell does not reliably execute `.py` files as `.\main.py`: `.PY` is absent from `PATHEXT` in the tested shell, and a harmless script did not run when invoked that way. Use `python .\main.py` or the direct `.venv` command below.

For direct execution without relying on a global Python command, use `.\.venv\Scripts\python.exe .\main.py`. To start by double-clicking in File Explorer, use `start.bat`; it calls the same launcher with the project `.venv` and keeps the terminal open after errors.

For manual two-window startup, start Lavalink in one PowerShell window:

```powershell
Set-Location .\lavalink
java -jar .\Lavalink.jar
```

Then, in another PowerShell window from the repository root, start Miku with its package entry point:

```powershell
.\.venv\Scripts\python.exe -m miku
```

The package entry point validates configuration, logs startup status without printing credential values, connects to Discord, and stays running until stopped with Ctrl+C. Slash commands are synced to `DISCORD_GUILD_ID` when set (useful during development); otherwise they sync globally. The installed `miku` console command is also available after package installation.

For a YouTube search, use `/play query: Kalyani` or `!p Kalyani`. `!play` remains an alias. Miku automatically selects the highest-ranked result Lavalink can load. A direct YouTube video URL plays that video, and supported playlist URLs queue their tracks. `/search` and `!search` display read-only results. YouTube may reject private, deleted, age-restricted, region-restricted, live, or otherwise unsupported videos; metadata availability does not guarantee playable audio.

## Linux or macOS

From the repository root, create a virtual environment and install the runtime package:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

If `.env` is missing, copy `.env.example` to `.env` and set `DISCORD_BOT_TOKEN` and the Lavalink values. Do not overwrite an existing `.env`. For a local server, start both processes with `python main.py`; for a remote server or bot-only startup, use `python -m miku`. Run an authorized Lavalink v4 server separately when using the remote endpoint and point `LAVALINK_URI` at an address Miku can reach.

For a local Lavalink server, `python main.py` from the repository root starts Lavalink and then Miku in the same terminal. `python -m miku` remains the standard bot-only entry point.

## Commands

| Command | Purpose |
| --- | --- |
| `/play query`, `!p query`, `!play query` | Play the best playable YouTube result; direct video URLs play the exact video |
| `/search query`, `!search query` | Show read-only YouTube metadata results |
| `/join`, `/leave` | Join or leave your voice channel |
| `/pause`, `/resume`, `/skip`, `/previous`, `/stop` | Control playback |
| `/queue`, `/nowplaying`, `/remove`, `/clear` | Inspect or manage the guild queue |
| `/volume`, `/loop`, `/shuffle` | Adjust playback settings |
| `/help` | Show available commands |
| `/sync`, `!sync` | Sync slash commands; bot owner only |

Prefix equivalents require `PREFIX_COMMANDS_ENABLED=true` and Message Content Intent. Owner-only checks remain active in every channel.

## Configuration

See `.env.example` for the template. `DISCORD_BOT_TOKEN` is required. `DISCORD_GUILD_ID` is optional and selects a development guild for immediate slash-command sync. `DISCORD_OWNER_ID` optionally sets the owner-only user ID. `YOUTUBE_API_KEY` is optional and adds official YouTube metadata; without it, Miku searches through Lavalink. `LAVALINK_URI` and `LAVALINK_PASSWORD` must be set together to enable Lavalink playback. Set `LAVALINK_SECURE=true` when connecting to a TLS endpoint using a bare host address. `COMMAND_PREFIX` defaults to `!`.

Voice commands are disabled by default. To enable them, install the optional Vosk receiver dependencies with `python -m pip install -e ".[voice]"`, install a compatible speech model locally, and set `VOICE_COMMANDS_ENABLED=true` and `VOSK_MODEL_PATH`. Consent is required before voice receiving starts. Voice receiving is an alpha feature and has not been verified against a live Discord server in this project.

Supported voice examples include “Miku, play Kalyani”, “Miku, search Kalyani”, “Miku, pause”, “Miku, skip”, “Miku, queue”, and “Miku, leave”. Voice commands select songs by query; they do not select a numbered search result.

## Development and tests

Install development tools with `python -m pip install -e ".[dev]"` and run the full suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

On Linux or macOS, activate `.venv` and run `python -m pytest -q`.

Tests mock Discord, Lavalink, and YouTube calls; they do not verify live Discord audio playback. The default test suite does not need Java or downloaded JAR files.

## Deployment

`pyproject.toml` defines package metadata, runtime dependencies, optional `dev` and `voice` extras, and the console entry point. `requirements.txt` mirrors the pinned runtime dependencies for platforms that require a requirements file. Install Python 3.10 or newer from the project root with `python -m pip install .`, or install those runtime packages with `python -m pip install -r requirements.txt`. Keep the repository root as the process working directory and start Miku with `python -m miku`; after installing the package, the standard `miku` console command is also available. Configure secrets through the platform's environment-variable settings. Do not bake `.env`, `lavalink/application.yml`, JARs, or speech models into a public image. The Lavalink service must be reachable from the bot host.

Miku reads settings at startup. Restart the Miku process after changing environment variables or deploying code. Restart Lavalink separately after changing its server configuration or plugin installation. In-memory playback queues are cleared when Miku restarts.

## Troubleshooting

- **`DISCORD_BOT_TOKEN is required`:** set the bot token in `.env` or the deployment environment, then restart Miku.
- **Discord rejects login or reports an intent:** verify the bot token and enable Message Content Intent only if prefix commands are enabled.
- **Commands do not appear:** check the `applications.commands` invite scope and the bot's guild access. A configured `DISCORD_GUILD_ID` syncs commands to that development guild.
- **Miku cannot connect to Lavalink:** start Lavalink, verify `LAVALINK_URI`, and make `LAVALINK_PASSWORD` match `lavalink.server.password`. Miku does not log either value.
- **Lavalink cannot start:** install Java 17 or newer and check that the YouTube plugin JAR is present under `lavalink/plugins/`. The setup script does not overwrite existing JARs or configuration.
- **A YouTube track fails:** inspect the Lavalink server logs for the specific video error. Miku marks a failed track and advances the queue; it does not switch providers or bypass YouTube restrictions.
- **Prefix commands do not respond:** set `PREFIX_COMMANDS_ENABLED=true` and enable Message Content Intent in the Developer Portal.

## Local files and secrets

`.env`, `lavalink/application.yml`, downloaded JARs, databases, logs, virtual environments, build outputs, and common private-key files are ignored by Git. Commit only the safe `.env.example` and `lavalink/application.yml.example` templates. Replace every placeholder locally before use.
