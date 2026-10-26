"""Ids and the one sequence number everything is ordered by.

`seq` is global and monotonic. Messages page by it (`before_seq`, `after_seq`),
and every stream frame carries it as its SSE `id:`, so one counter is also the
resume point. It is persisted with the rest of the state, so a restart does not
hand out numbers a client has already seen.
"""

from __future__ import annotations

import secrets
import threading
import time

_lock = threading.Lock()
_seq = 0


def seed_seq(value: int) -> None:
    """Continue from a stored high-water mark at boot."""
    global _seq
    with _lock:
        _seq = max(_seq, int(value or 0))


def next_seq() -> int:
    global _seq
    with _lock:
        _seq += 1
        return _seq


def current_seq() -> int:
    with _lock:
        return _seq


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def token(n: int = 8) -> str:
    return secrets.token_hex(n // 2)


def message_id() -> str:
    """`<unix seconds>.<seq>`, which is also a thread's id when a thread is rooted on it.

    The UI anchors a thread's card to the timeline row whose id equals the
    thread's `ts`, so the two must be the same string.
    """
    return f"{int(time.time())}.{next_seq():06d}"


def chat_id(project: str | None) -> str:
    return "chat_workspace" if not project else f"chat_{project}"


def session_id() -> str:
    return f"cse_{token(16)}"


def review_id() -> str:
    return f"rv_{token(16)}"


def hop_id() -> str:
    return f"hop_{token(12)}"


def run_id() -> str:
    return f"run_{token(16)}"


def turn_id() -> str:
    return f"trn_{token(16)}"
