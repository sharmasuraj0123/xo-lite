"""The store: chats, messages, threads, sessions, review items, hops and runs.

One JSON file for the index and one append-only JSONL per chat for its messages.
No database, because there is nothing here a database would do better: the whole
workspace is a few thousand rows and one process owns it.

Everything that mutates goes through this module, and every mutation that a
client should see broadcasts a frame (see `xolite/stream.py`). Keeping those two
together is deliberate — a write the stream does not announce is invisible to the
UI until the page is reloaded, since the client never polls for it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from . import config, ids

_lock = threading.RLock()

# The in-memory index. Written to state.json after every change.
STATE: dict[str, Any] = {
    "chats": {},     # chat id   → chat row
    "threads": {},   # thread ts → thread row
    "sessions": {},  # cse id    → session row
    "review": {},    # rv id     → review item
    "hops": {},      # hop id    → hop row
    "runs": {},      # run id    → run row
    "pools": {},
    "router": {"mode": "auto", "model": None, "provider": "xo-lite", "connected": True},
    "seq": 0,
}

# chat id → list of message rows, kept in memory and mirrored to disk.
MESSAGES: dict[str, list[dict]] = {}


# ── persistence ──────────────────────────────────────────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save() -> None:
    with _lock:
        STATE["seq"] = ids.current_seq()
        _atomic_write(config.STATE_FILE, json.dumps(STATE, indent=1))


def _messages_path(chat_id: str) -> Path:
    safe = "".join(c for c in chat_id if c.isalnum() or c in "-_.")
    return config.MESSAGES_DIR / f"{safe}.jsonl"


def _append_message(row: dict) -> None:
    with open(_messages_path(row["chat_id"]), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def load() -> None:
    """Read the store back, or start an empty one."""
    config.ensure_dirs()
    with _lock:
        if config.STATE_FILE.exists():
            try:
                stored = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
                for key in STATE:
                    if key in stored:
                        STATE[key] = stored[key]
            except (ValueError, OSError):
                pass  # a corrupt index is not worth crashing over; start fresh
        ids.seed_seq(STATE.get("seq", 0))
        MESSAGES.clear()
        for path in config.MESSAGES_DIR.glob("*.jsonl"):
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            if rows:
                MESSAGES[rows[0]["chat_id"]] = rows


# ── projects and chats ───────────────────────────────────────────────────────
def discover_projects() -> list[str]:
    """Every direct child folder of the projects root is a project.

    The workspace chat always exists. A project that disappears from disk keeps
    its chat (and its history) rather than being deleted — the UI shows it, and
    the owner can retire it deliberately.
    """
    found: list[str] = []
    root = config.PROJECTS_ROOT
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                found.append(child.name)
    with _lock:
        ensure_chat(None)
        for project in found:
            ensure_chat(project)
    return found


def ensure_chat(project: str | None) -> dict:
    cid = ids.chat_id(project)
    with _lock:
        chat = STATE["chats"].get(cid)
        if chat:
            return chat
        chat = {
            "id": cid,
            "kind": "workspace" if project is None else "project",
            "project": project,
            "project_pid": f"proj_{project}" if project else None,
            "routing": {"mode": "auto"},
            "pin": {"agent": config.AGENT_NAME, "model": config.AGENT_MODEL, "effort": "medium"},
            "retired_at": None,
            "created_at": ids.now_iso(),
        }
        STATE["chats"][cid] = chat
        return chat


def chats() -> list[dict]:
    with _lock:
        return list(STATE["chats"].values())


def chat(cid: str) -> dict | None:
    return STATE["chats"].get(cid)


def project_dir(project: str | None) -> Path:
    return config.PROJECTS_ROOT if project is None else config.PROJECTS_ROOT / project


def project_of(chat_id: str) -> str | None:
    row = STATE["chats"].get(chat_id)
    return row.get("project") if row else None


# ── messages ─────────────────────────────────────────────────────────────────
def add_message(
    chat_id: str,
    *,
    text: str,
    author_kind: str,
    author_id: str | None = None,
    thread_ts: str | None = None,
    extra: dict | None = None,
    reply_to: str | None = None,
    message_id: str | None = None,
) -> dict:
    """Store one message. The caller broadcasts the frame."""
    with _lock:
        row = {
            "id": message_id or ids.message_id(),
            "chat_id": chat_id,
            "seq": ids.next_seq(),
            "created_at": ids.now_iso(),
            "author_kind": author_kind,
            "author_id": author_id,
            "text": text,
            "thread_ts": thread_ts,
            "reply_to": reply_to,
            "extra": extra or {},
        }
        MESSAGES.setdefault(chat_id, []).append(row)
        _append_message(row)
        return row


def messages(
    chat_id: str,
    *,
    thread: str | None = None,
    before_seq: int | None = None,
    after_seq: int | None = None,
    limit: int = 40,
) -> dict:
    """A page of a chat's messages, newest-last, with the cursors the client pages by."""
    with _lock:
        rows = [r for r in MESSAGES.get(chat_id, []) if (r.get("thread_ts") or None) == (thread or None)]
    rows.sort(key=lambda r: r.get("seq") or 0)
    if after_seq is not None:
        rows = [r for r in rows if (r.get("seq") or 0) > after_seq]
        page = rows[: max(1, limit)]
    else:
        if before_seq is not None:
            rows = [r for r in rows if (r.get("seq") or 0) < before_seq]
        page = rows[-max(1, limit):]
    older = bool(rows) and bool(page) and rows[0]["seq"] < page[0]["seq"]
    return {
        "messages": page,
        "next_before_seq": page[0]["seq"] if (page and older) else None,
        "next_after_seq": page[-1]["seq"] if page else None,
    }


def message(chat_id: str, message_id: str) -> dict | None:
    for row in MESSAGES.get(chat_id, []):
        if row["id"] == message_id:
            return row
    return None


# ── threads ──────────────────────────────────────────────────────────────────
def add_thread(
    *,
    ts: str,
    chat_id: str,
    title: str,
    label: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
) -> dict:
    with _lock:
        row = {
            "ts": ts,
            "chat_id": chat_id,
            "title": title[:120] or "Thread",
            "label": label,
            "state": "working",
            "card": {"happened": "", "needs_you": "", "updated_at": ids.now_iso()},
            "branch": None,
            "worktree_path": None,
            "cwd": cwd,
            "created_at": ids.now_iso(),
            "updated_at": ids.now_iso(),
            "status_md": "",
            "session_id": session_id,
            "session": None,
            "resolved_at": None,
            "agent_ms": 0,
            "pin": {"agent": config.AGENT_NAME, "model": config.AGENT_MODEL, "effort": "medium"},
            "runs": {"running": 0, "waiting": 0, "total": 0},
            "outputs": [],
        }
        STATE["threads"][ts] = row
        return row


def thread(ts: str) -> dict | None:
    return STATE["threads"].get(ts)


def threads_of(chat_id: str) -> list[dict]:
    return [t for t in STATE["threads"].values() if t["chat_id"] == chat_id]


def update_thread(ts: str, **fields) -> dict | None:
    with _lock:
        row = STATE["threads"].get(ts)
        if not row:
            return None
        row.update(fields)
        row["updated_at"] = ids.now_iso()
        return row


def set_card(ts: str, *, happened: str | None = None, needs_you: str | None = None) -> dict | None:
    with _lock:
        row = STATE["threads"].get(ts)
        if not row:
            return None
        card = dict(row.get("card") or {})
        if happened is not None:
            card["happened"] = happened
        if needs_you is not None:
            card["needs_you"] = needs_you
        card["updated_at"] = ids.now_iso()
        row["card"] = card
        row["updated_at"] = card["updated_at"]
        return row


# ── sessions ─────────────────────────────────────────────────────────────────
def add_session(*, role: str, chat_id: str | None, thread_ts: str | None = None) -> dict:
    with _lock:
        row = {
            "id": ids.session_id(),
            "role": role,
            "chat_id": chat_id,
            "thread_ts": thread_ts,
            "state": "idle",
            "agent": config.AGENT_NAME,
            "model": config.AGENT_MODEL,
            "effort": "medium",
            "paused": None,
            "retired_at": None,
            "live_runs": 0,
            "seq": ids.next_seq(),
        }
        STATE["sessions"][row["id"]] = row
        return row


def session(sid: str) -> dict | None:
    return STATE["sessions"].get(sid)


def sessions() -> list[dict]:
    return list(STATE["sessions"].values())


def coordinator_of(chat_id: str) -> dict:
    """The chat's coordinator session, made on first use.

    Coordinators here are long-lived, unlike xo-space's, which are recycled after
    a number of turns because an LLM's context fills up. A rules engine has no
    context to fill, so there is nothing to recycle.
    """
    kind = STATE["chats"].get(chat_id, {}).get("kind")
    role = "workspace-coordinator" if kind == "workspace" else "project-coordinator"
    with _lock:
        for row in STATE["sessions"].values():
            if row["chat_id"] == chat_id and row["role"] == role and not row["retired_at"]:
                return row
        return add_session(role=role, chat_id=chat_id)


def update_session(sid: str, **fields) -> dict | None:
    with _lock:
        row = STATE["sessions"].get(sid)
        if not row:
            return None
        row.update(fields)
        row["seq"] = ids.next_seq()
        return row


# ── review ───────────────────────────────────────────────────────────────────
def add_review(item: dict) -> dict:
    with _lock:
        item.setdefault("id", ids.review_id())
        item.setdefault("state", "open")
        item.setdefault("created_at", ids.now_iso())
        item.setdefault("risk", "notable")
        item.setdefault("risk_reasons", [])
        item.setdefault("blocking", False)
        item.setdefault("detail", {})
        item.setdefault("options", [])
        STATE["review"][item["id"]] = item
        return item


def review_item(rid: str) -> dict | None:
    return STATE["review"].get(rid)


def review(state: str = "open") -> list[dict]:
    return [i for i in STATE["review"].values() if i.get("state", "open") == state]


def close_review(rid: str, decision: str, note: str | None = None) -> dict | None:
    with _lock:
        item = STATE["review"].get(rid)
        if not item:
            return None
        item["state"] = "decided"
        item["decision"] = decision
        item["note"] = note
        item["decided_at"] = ids.now_iso()
        return item


# ── hops ─────────────────────────────────────────────────────────────────────
def add_hop(*, kind: str, chat_id: str, from_chat_id: str | None, message_id: str | None,
            source_message_id: str | None = None, thread_ts: str | None = None) -> dict:
    with _lock:
        row = {
            "id": ids.hop_id(),
            "kind": kind,
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
            "source_message_id": source_message_id,
            "thread_ts": thread_ts,
            "via": ["workspace-coordinator"] if kind == "forward" else [],
            "created_at": ids.now_iso(),
            "seq": ids.next_seq(),
        }
        STATE["hops"][row["id"]] = row
        return row


def hops(chat_id: str, thread: str | None = None) -> list[dict]:
    rows = [h for h in STATE["hops"].values() if h["chat_id"] == chat_id or h.get("from_chat_id") == chat_id]
    if thread:
        rows = [h for h in rows if h.get("thread_ts") == thread]
    return sorted(rows, key=lambda h: h.get("seq") or 0)


# ── runs ─────────────────────────────────────────────────────────────────────
def add_run(*, session_id: str, title: str, kind: str = "subagent", parent: str | None = None) -> dict:
    with _lock:
        row = {
            "id": ids.run_id(),
            "session": session_id,
            "parent_run_id": parent,
            "state": "running",
            "title": title,
            "agent_type": kind,
            "kind": kind,
            "background": False,
            "depth": 2 if parent else 1,
            "started_at": ids.now_iso(),
            "ended_at": None,
            "summary": None,
            "tokens": 0,
            "tool_calls": 0,
            "last_tool": None,
            "can_stop": True,
            "children": [],
        }
        STATE["runs"][row["id"]] = row
        return row


def runs_of(session_id: str) -> dict:
    rows = [r for r in STATE["runs"].values() if r["session"] == session_id]
    by_id = {r["id"]: dict(r, children=[]) for r in rows}
    tree = []
    for row in by_id.values():
        parent = by_id.get(row.get("parent_run_id") or "")
        (parent["children"] if parent else tree).append(row)
    counts: dict[str, int] = {}
    for row in rows:
        key = {"running": "running", "waiting": "waiting", "finished": "done"}.get(row["state"], row["state"])
        counts[key] = counts.get(key, 0) + 1
    return {"runs": tree, "counts": counts}


def run(rid: str) -> dict | None:
    return STATE["runs"].get(rid)


def update_run(rid: str, **fields) -> dict | None:
    with _lock:
        row = STATE["runs"].get(rid)
        if row:
            row.update(fields)
        return row


def iter_all(*keys: str) -> Iterable[dict]:
    for key in keys:
        yield from STATE[key].values()
