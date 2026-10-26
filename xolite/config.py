"""Where things live and which knobs exist. Read once at import.

Everything the server owns is machine-local under XO_LITE_HOME (default
~/.xo-lite), the same convention xo-space follows: nothing is written into a
project except its git worktrees, and nothing goes in `.xo/`.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


HOST = _env("XO_LITE_HOST", "127.0.0.1")
PORT = int(_env("XO_LITE_PORT", "5002"))

# The store. One folder, inspectable, no database.
HOME = Path(_env("XO_LITE_HOME", str(Path.home() / ".xo-lite"))).expanduser()
STATE_FILE = HOME / "state.json"
MESSAGES_DIR = HOME / "messages"
FRAMES_FILE = HOME / "frames.jsonl"
LOGS_DIR = HOME / "logs"

# Where projects are looked for. A project is any direct child folder.
PROJECTS_ROOT = Path(_env("XO_LITE_PROJECTS", str(Path.home() / "xo-projects"))).expanduser()

# Which engine decides what happens to a message. The seam is xolite/engine/base.py;
# "rules" is the deterministic one that ships here. An LLM-backed engine would be
# another module exposing the same run_turn().
ENGINE = _env("XO_LITE_ENGINE", "rules")

# Threads get a git worktree of their project, under this subfolder.
WORKTREES_SUBDIR = _env("XO_LITE_WORKTREES", ".xo-worktrees")
WORKTREES_ENABLED = _flag("XO_LITE_WORKTREES_ON", True)

# How many turns may run at once, and how long one may take.
MAX_TURNS = int(_env("XO_LITE_MAX_TURNS", "4"))
TURN_TIMEOUT_S = float(_env("XO_LITE_TURN_TIMEOUT", "120"))

# How long a turn waits before it answers, so the UI's "Replying" is visible.
# Set to 0 for tests.
TURN_DELAY_S = float(_env("XO_LITE_TURN_DELAY", "1.2"))

# The stream keeps this many frames in memory for a Last-Event-ID resume; older
# ones are read back from frames.jsonl.
FRAME_RING = int(_env("XO_LITE_FRAME_RING", "2000"))
HEARTBEAT_S = float(_env("XO_LITE_HEARTBEAT", "15"))

# What the UI shows on its model chip. Nothing here calls a model; these are
# labels for a local engine, and they are honest about that.
AGENT_NAME = _env("XO_LITE_AGENT", "xo_lite")
AGENT_LABEL = _env("XO_LITE_AGENT_LABEL", "XO Lite (rules)")
# What the UI prints on its model chip. There is no model; this names the engine
# so the chip reads honestly instead of showing an empty slot.
AGENT_MODEL = _env("XO_LITE_MODEL", "rules-v1")


def ensure_dirs() -> None:
    for path in (HOME, MESSAGES_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)
