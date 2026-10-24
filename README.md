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

Start Lavalink in one PowerShell window:

```powershell
Set-Location .\lavalink
java -jar .\Lavalink.jar
```

From the repository root, start Miku in another window:

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

If `.env` is missing, copy `.env.example` to `.env` and set `DISCORD_BOT_TOKEN` and the Lavalink values. Do not overwrite an existing `.env`. Start Miku with `python -m miku`. Run your authorized Lavalink v4 server separately and point `LAVALINK_URI` at an address Miku can reach.

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

Install Python 3.10 or newer and the production package with `python -m pip install .`. Configure the token and Lavalink connection through the host's secret/environment-variable settings, then use `python -m miku` as the process command. Do not bake `.env`, `lavalink/application.yml`, JARs, or speech models into a public image. The Lavalink service must be reachable from the bot host. Use `requirements.txt` for tools that require a requirements file; it installs the local package and its runtime dependencies without test tools.

### ACLClouds with the repository in a subdirectory

ACLClouds can run pip from `/home/container/` while the checkout is under `/home/container/Miku-main/`. Set its **Requirements File** to `Miku-main/requirements.txt`; the file lists pinned packages directly and does not depend on pip's current directory. Set **Main File** to `Miku-main/aclclouds.py`. This small adapter changes to the project directory and delegates to the same `miku.bot.main` used by `python -m miku`. Add `DISCORD_BOT_TOKEN` and the required Lavalink values as private ACLClouds startup variables. Do not put credentials in either file.

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
