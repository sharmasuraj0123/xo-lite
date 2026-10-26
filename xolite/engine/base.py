"""The seam: what decides where a message goes, and how it answers.

xo-space hands a turn to an agent CLI and hears the agent only through MCP
"comms" tools — `forward_to_project`, `start_thread_session`, `message_thread`.
The agent's whole job there is choosing which tool to call. Everything after the
call is server code.

So the model is not load-bearing, and this module makes that explicit: an engine
is given a `TurnSpec` and returns `ToolCall`s. The rules engine that ships here
decides by pattern. An LLM-backed engine would be another module with the same
`run_turn`, and nothing else in the server would change.

What an engine may NOT do is as important as what it may: it never writes to the
store, never speaks as the owner, and never invents who said what. It returns
calls; `xolite/delegation/turns.py` applies them. That is the same rule xo-space
states as "who said what is decided by the server".
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

# Roles a turn can run as.
WORKSPACE = "workspace-coordinator"
PROJECT = "project-coordinator"
THREAD = "thread"

# The tool vocabulary, per role. A call to a terminal tool ends the turn.
TOOLS: dict[str, list[str]] = {
    WORKSPACE: ["forward_to_project", "reply", "ask_owner"],
    PROJECT: ["start_thread_session", "message_thread", "reply", "ask_owner"],
    THREAD: ["update_status", "note", "spawn", "ask_owner", "finish", "reply"],
}

TERMINAL: dict[str, set[str]] = {
    WORKSPACE: {"forward_to_project", "reply", "ask_owner"},
    PROJECT: {"start_thread_session", "message_thread", "reply", "ask_owner"},
    THREAD: {"ask_owner", "finish", "reply"},
}


@dataclass
class ProjectFacts:
    """What an engine is told about one project. Read-only, built from the store."""

    name: str
    chat_id: str
    open_threads: list[dict] = field(default_factory=list)
    recent_titles: list[str] = field(default_factory=list)


@dataclass
class TurnSpec:
    """One turn's input. Everything an engine may see, and nothing it may change."""

    role: str
    session_id: str
    chat_id: str
    thread_ts: str | None
    cwd: str
    # The message that woke this turn, as the store holds it.
    message: dict | None
    # The thread's own row, when the role is `thread`.
    thread: dict | None = None
    # Every project the workspace knows, for routing.
    projects: list[ProjectFacts] = field(default_factory=list)
    # This chat's recent messages, oldest first, for context.
    history: list[dict] = field(default_factory=list)
    turn_index: int = 0

    @property
    def text(self) -> str:
        return str((self.message or {}).get("text") or "")

    @property
    def terminal_tools(self) -> set[str]:
        return TERMINAL.get(self.role, set())


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    # Why the engine chose this, in the engine's own words. Logged, and shown on
    # a decision's card when the call opens one, so a routing choice is never
    # unexplained.
    why: str = ""


@dataclass
class TurnResult:
    ok: bool = True
    calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None

    def terminal(self, role: str) -> ToolCall | None:
        for call in self.calls:
            if call.tool in TERMINAL.get(role, set()):
                return call
        return None


class Engine(Protocol):
    """What a chat engine must provide. One function, plus the roles it can run."""

    SUPPORTED_ROLES: list[str]

    async def run_turn(self, spec: TurnSpec) -> TurnResult: ...


def load(name: str):
    """Load the engine named in XO_LITE_ENGINE. A bad name fails loudly at boot."""
    module = importlib.import_module(f".{name}", __package__)
    for required in ("SUPPORTED_ROLES", "run_turn"):
        if not hasattr(module, required):
            raise RuntimeError(f"engine '{name}' is missing {required}")
    return module
