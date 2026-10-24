"""Start local Lavalink when configured, then run Miku's package entry point."""

from __future__ import annotations

import ipaddress
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from miku.config import Settings


PROJECT_ROOT = Path(__file__).resolve().parent
LAVALINK_DIR = PROJECT_ROOT / "lavalink"
LAVALINK_JAR = LAVALINK_DIR / "Lavalink.jar"
LAVALINK_CONFIG = LAVALINK_DIR / "application.yml"
STARTUP_TIMEOUT_SECONDS = 90


def _lavalink_target(uri: str) -> tuple[str, int]:
    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("LAVALINK_URI must be an http(s) URL or host and port.")
    try:
        port = parsed.port or 2333
    except ValueError as exc:
        raise ValueError("LAVALINK_URI must contain a valid port.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("LAVALINK_URI port must be between 1 and 65535.")
    return parsed.hostname, port


def _is_local_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_for_lavalink(
    host: str,
    port: int,
    process: subprocess.Popen,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Lavalink exited during startup (exit code {return_code}).")
        if _port_is_open(host, port):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Lavalink did not open {host}:{port} within {timeout:g} seconds. "
                "Check its startup output and local configuration."
            )
        time.sleep(min(0.5, remaining))


def _stop_lavalink(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except OSError:
        # The process may have exited between poll() and terminate().
        return


def _run_miku() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "miku"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return result.returncode


def main() -> int:
    try:
        settings = Settings.from_env()
        settings.validate()
        if not settings.lavalink_configured:
            raise ValueError(
                "Set both LAVALINK_URI and LAVALINK_PASSWORD before using the combined launcher."
            )
        host, port = _lavalink_target(settings.lavalink_connection_uri)
    except (OSError, ValueError) as exc:
        print(f"Miku launcher configuration error: {exc}", file=sys.stderr)
        return 2

    if not _is_local_host(host):
        print("Using the configured remote Lavalink server; starting Miku.")
        return _run_miku()

    if _port_is_open(host, port):
        print("Local Lavalink is already reachable; starting Miku.")
        return _run_miku()

    java = shutil.which("java")
    if java is None:
        print("Java 17 or newer is required to start local Lavalink.", file=sys.stderr)
        return 2
    if not LAVALINK_JAR.is_file() or not LAVALINK_CONFIG.is_file():
        print(
            "Local Lavalink files are missing. Run scripts/setup-windows.ps1 "
            "and review the generated configuration first.",
            file=sys.stderr,
        )
        return 2

    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            [java, "-jar", str(LAVALINK_JAR)],
            cwd=LAVALINK_DIR,
        )
        print("Starting local Lavalink; its logs will appear in this window.")
        _wait_for_lavalink(host, port, process)
        print("Lavalink is reachable; starting Miku.")
        return _run_miku()
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"Miku launcher could not start Lavalink: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nMiku launcher stopped.")
        return 130
    finally:
        if process is not None:
            _stop_lavalink(process)


if __name__ == "__main__":
    raise SystemExit(main())
