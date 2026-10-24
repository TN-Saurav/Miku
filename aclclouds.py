"""ACLClouds file-entry adapter; the bot startup logic stays in miku.bot."""

from __future__ import annotations

import os
from pathlib import Path


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    from miku.bot import main

    raise SystemExit(main())
