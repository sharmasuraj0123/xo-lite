"""The one global stream.

Every change the UI should see arrives here as a frame, because the client does
not poll: `watch()` and `watchThread()` in the UI are one-shot loads, so a
mutation that broadcasts nothing stays invisible until the page is reloaded.
That makes this module the contract, not a convenience.

Frames are numbered with the same global `seq` as messages, which is also the
SSE `id:`, so `Last-Event-ID` resumes exactly where a dropped connection left
off. Recent frames are kept in memory; older ones are read back from
frames.jsonl.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from . import config, ids

_subscribers: list[asyncio.Queue] = []
_ring: deque[dict] = deque(maxlen=config.FRAME_RING)


def _persist(frame: dict) -> None:
    try:
        with open(config.FRAMES_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(frame) + "\n")
    except OSError:
        pass  # a full disk should not stop the server answering


def envelope(event: str, data: dict, **where: Any) -> dict:
    seq = ids.next_seq()
    return {
        "type": event,
        "id": str(seq),
        "seq": seq,
        "ts": ids.now_iso(),
        "chat_id": where.get("chat_id"),
        "session_id": where.get("session_id"),
        "thread_ts": where.get("thread_ts"),
        "turn_id": where.get("turn_id"),
        "run_id": where.get("run_id"),
        "data": data,
    }


def broadcast(event: str, data: dict, **where: Any) -> dict:
    """Send one frame to every open stream. Safe to call from any request handler."""
    frame = envelope(event, data, **where)
    _ring.append(frame)
    _persist(frame)
    for queue in list(_subscribers):
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass  # that tab is behind; it resumes with Last-Event-ID
    return frame


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    _subscribers.append(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    if queue in _subscribers:
        _subscribers.remove(queue)


def replay(after_seq: int) -> list[dict]:
    """Frames after `after_seq`, from memory if they are still there, else from disk."""
    if _ring and _ring[0]["seq"] <= after_seq + 1:
        return [f for f in _ring if f["seq"] > after_seq]
    out: list[dict] = []
    try:
        with open(config.FRAMES_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if frame.get("seq", 0) > after_seq:
                    out.append(frame)
    except OSError:
        pass
    return out


def _format(event: str, data: dict, event_id: str | None = None) -> bytes:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def sse(last_event_id: str | None = None):
    """One Server-Sent Events connection: snapshot, any missed frames, then live."""
    queue = subscribe()
    try:
        yield _format("snapshot", {
            "type": "snapshot", "id": None, "seq": None, "ts": ids.now_iso(),
            "chat_id": None, "session_id": None, "thread_ts": None,
            "turn_id": None, "run_id": None,
            "data": {"seq": ids.current_seq(), "turns": []},
        })

        if last_event_id:
            # "N" or "N:k" — the client sends back exactly what it last saw.
            try:
                after = int(str(last_event_id).split(":")[0])
            except ValueError:
                after = 0
            for frame in replay(after):
                yield _format(frame["type"], frame, frame["id"])

        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=config.HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield _format("heartbeat", {"type": "heartbeat", "ts": ids.now_iso(), "data": {}})
                continue
            yield _format(frame["type"], frame, frame["id"])
    finally:
        unsubscribe(queue)


# ── the frames this server sends ─────────────────────────────────────────────
def message(row: dict) -> None:
    broadcast("message", row, chat_id=row["chat_id"], thread_ts=row.get("thread_ts"))


def thread(row: dict) -> None:
    broadcast("thread", row, chat_id=row["chat_id"], thread_ts=row["ts"])


def card(row: dict) -> None:
    broadcast("card", {"ts": row["ts"], "card": row["card"]}, chat_id=row["chat_id"], thread_ts=row["ts"])


def session(row: dict, reason: str, **extra: Any) -> None:
    data = {"status": row["state"], "reason": reason, "role": row["role"], "agent": row["agent"], **extra}
    broadcast("session", data, chat_id=row.get("chat_id"), session_id=row["id"], thread_ts=row.get("thread_ts"))


def chat(row: dict) -> None:
    broadcast("chat", row, chat_id=row["id"])


def hop(row: dict) -> None:
    broadcast("hop", row, chat_id=row["chat_id"], thread_ts=row.get("thread_ts"))


def review_opened(item: dict) -> None:
    broadcast("review.opened", item, chat_id=item.get("chat_id"), thread_ts=item.get("thread_ts"))


def review_decided(item: dict) -> None:
    broadcast("review.decided", {"id": item["id"], "decision": item.get("decision")},
              chat_id=item.get("chat_id"), thread_ts=item.get("thread_ts"))


def run_started(row: dict) -> None:
    broadcast("run.started", {
        "run_id": row["id"], "parent_run_id": row.get("parent_run_id"), "state": row["state"],
        "info": {"title": row["title"], "agent_type": row["agent_type"], "kind": row["kind"],
                 "depth": row["depth"], "background": row["background"]},
    }, session_id=row["session"], run_id=row["id"])


def run_finished(row: dict) -> None:
    broadcast("run.finished", {
        "run_id": row["id"], "state": row["state"], "status": row.get("status"),
        "summary": row.get("summary"), "tokens": row.get("tokens"), "tool_calls": row.get("tool_calls"),
    }, session_id=row["session"], run_id=row["id"])
