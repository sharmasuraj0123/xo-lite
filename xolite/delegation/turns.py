"""The turn engine: wakes a session, asks the engine what to do, and does it.

The split is the point. The engine (`xolite/engine/`) decides and returns tool
calls; this module is the only thing that writes to the store and the only thing
that speaks on the stream. So an engine cannot invent a message from the owner,
cannot forge who said what, and cannot change a thread behind the server's back —
it can only ask for one of a fixed set of calls.

A turn runs when something wakes it: an owner message, a relay from another
chat, or a decision being answered. Idle costs nothing; there is no loop.
"""

from __future__ import annotations

import asyncio
import json
import traceback

from .. import config, ids, store, stream
from ..engine import base
from ..engine.base import ProjectFacts, ToolCall, TurnResult, TurnSpec
from . import worktree

_engine = None
_gate: asyncio.Semaphore | None = None
_turns: dict[str, int] = {}     # session id → how many turns it has run
_running: set[str] = set()      # session ids with a turn in flight


def engine():
    global _engine
    if _engine is None:
        _engine = base.load(config.ENGINE)
    return _engine


def gate() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(config.MAX_TURNS)
    return _gate


def _log(session_id: str, line: str) -> None:
    try:
        with open(config.LOGS_DIR / f"{session_id}.log", "a", encoding="utf-8") as fh:
            fh.write(f"{ids.now_iso()} {line}\n")
    except OSError:
        pass


# ── what the engine is told ──────────────────────────────────────────────────
def _facts_for(chat_id: str) -> list[ProjectFacts]:
    """Read-only facts about projects, built from the store."""
    out = []
    for chat in store.chats():
        if chat["kind"] != "project" or chat.get("retired_at"):
            continue
        threads = store.threads_of(chat["id"])
        out.append(ProjectFacts(
            name=chat["project"],
            chat_id=chat["id"],
            open_threads=[t for t in threads if t["state"] not in ("resolved", "done")],
            recent_titles=[t["title"] for t in sorted(threads, key=lambda t: t["updated_at"], reverse=True)[:8]],
        ))
    # A project coordinator is told about its own project first, so the engine can
    # read spec.projects[0] without knowing how the list was built.
    out.sort(key=lambda f: (f.chat_id != chat_id, f.name))
    return out


def _spec(session: dict, message: dict | None) -> TurnSpec:
    chat_id = session.get("chat_id") or ""
    thread_ts = session.get("thread_ts")
    thread = store.thread(thread_ts) if thread_ts else None
    project = store.project_of(chat_id)
    cwd = (thread or {}).get("worktree_path") or str(store.project_dir(project))
    history = store.messages(chat_id, thread=thread_ts, limit=12)["messages"] if chat_id else []
    return TurnSpec(
        role=session["role"],
        session_id=session["id"],
        chat_id=chat_id,
        thread_ts=thread_ts,
        cwd=cwd,
        message=message,
        thread=thread,
        projects=_facts_for(chat_id),
        rules=[r for r in store.rules(include_revoked=False)
               if r.get("scope") == "space"
               or (r.get("scope") == "thread" and r.get("thread_ts") == thread_ts)
               or (r.get("scope") == "project" and r.get("chat_id") == chat_id)],
        history=history,
        turn_index=_turns.get(session["id"], 0),
    )


# ── waking ───────────────────────────────────────────────────────────────────
def wake(session: dict, message: dict | None) -> None:
    """Schedule a turn. Two wakes for one session run one after the other."""
    asyncio.get_event_loop().create_task(_run(session["id"], message))


async def _run(session_id: str, message: dict | None) -> None:
    # One turn per session: a second wake waits rather than racing.
    while session_id in _running:
        await asyncio.sleep(0.05)
    _running.add(session_id)
    session = store.session(session_id)
    if not session:
        _running.discard(session_id)
        return
    async with gate():
        try:
            store.update_session(session_id, state="running")
            stream.session(store.session(session_id), "started")
            if config.TURN_DELAY_S:
                await asyncio.sleep(config.TURN_DELAY_S)

            spec = _spec(session, message)
            _log(session_id, f"turn role={spec.role} text={spec.text[:160]!r}")
            try:
                result = await asyncio.wait_for(engine().run_turn(spec), timeout=config.TURN_TIMEOUT_S)
            except asyncio.TimeoutError:
                result = TurnResult(ok=False, error=f"the turn took longer than {config.TURN_TIMEOUT_S:g}s")
            except Exception as exc:  # an engine bug must not take the server with it
                _log(session_id, "engine raised:\n" + traceback.format_exc())
                result = TurnResult(ok=False, error=f"{type(exc).__name__}: {exc}")

            _turns[session_id] = _turns.get(session_id, 0) + 1
            if not result.ok:
                _fail(spec, result.error or "the turn failed")
            else:
                for call in result.calls:
                    _log(session_id, f"  call {call.tool} {json.dumps(call.args)[:200]} — {call.why}")
                    apply_call(spec, call)
        finally:
            store.update_session(session_id, state="idle")
            stream.session(store.session(session_id), "finished")
            store.save()
            _running.discard(session_id)


def _fail(spec: TurnSpec, why: str) -> None:
    if spec.thread_ts:
        row = store.update_thread(spec.thread_ts, state="failed")
        store.set_card(spec.thread_ts, happened=f"The turn stopped: {why}")
        if row:
            stream.thread(row)
    else:
        row = store.add_message(spec.chat_id, text=f"That did not go through: {why}",
                                author_kind="system", author_id="system")
        stream.message(row)


# ── doing what the engine asked ──────────────────────────────────────────────
def apply_call(spec: TurnSpec, call: ToolCall) -> None:
    handler = _HANDLERS.get(call.tool)
    if not handler:
        _log(spec.session_id, f"  ! unknown tool {call.tool}")
        return
    if call.tool not in base.TOOLS.get(spec.role, []):
        _log(spec.session_id, f"  ! {spec.role} may not call {call.tool}")
        return
    handler(spec, call.args, call.why)


def _coordinator_name(chat_id: str) -> str:
    return "workspace-coordinator" if store.chat(chat_id) and store.chat(chat_id)["kind"] == "workspace" \
        else "project-coordinator"


def _reply(spec: TurnSpec, args: dict, why: str) -> None:
    row = store.add_message(
        spec.chat_id, text=str(args.get("text") or ""),
        author_kind="agent",
        author_id="thread-agent" if spec.role == base.THREAD else _coordinator_name(spec.chat_id),
        thread_ts=spec.thread_ts,
    )
    stream.message(row)


def _note(spec: TurnSpec, args: dict, why: str) -> None:
    _reply(spec, args, why)


def _forward_to_project(spec: TurnSpec, args: dict, why: str) -> None:
    """The workspace sends a message on. The copy is made by the server, never retyped."""
    project = str(args.get("project") or "")
    target = store.chat(ids.chat_id(project))
    if not target:
        return
    original = spec.message or {}
    copy = store.add_message(
        target["id"], text=original.get("text", ""), author_kind="owner",
        extra={"forwarded_from": {"chat_id": spec.chat_id, "message_id": original.get("id"),
                                  "via": ["workspace-coordinator"]},
               **({"attachments": original["extra"]["attachments"]}
                  if original.get("extra", {}).get("attachments") else {})},
    )
    stream.message(copy)
    hop = store.add_hop(kind="forward", chat_id=target["id"], from_chat_id=spec.chat_id,
                        message_id=copy["id"], source_message_id=original.get("id"))
    stream.hop(hop)

    receipt = store.add_message(
        spec.chat_id, text=f"Sent to **{project}** — {why}.", author_kind="agent",
        author_id="workspace-coordinator",
    )
    stream.message(receipt)

    # and the project's own coordinator picks it up
    wake(store.coordinator_of(target["id"]), copy)


def _start_thread_session(spec: TurnSpec, args: dict, why: str) -> None:
    """Open a thread: its own session, its own worktree, rooted on the message that asked."""
    original = spec.message or {}
    root_id = original.get("id") or ids.message_id()
    project = store.project_of(spec.chat_id)
    session = store.add_session(role=base.THREAD, chat_id=spec.chat_id, thread_ts=root_id)

    row = store.add_thread(
        ts=root_id, chat_id=spec.chat_id, title=str(args.get("title") or "Thread"),
        label=args.get("label"), cwd=str(store.project_dir(project)), session_id=session["id"],
    )
    tree = worktree.create(store.project_dir(project), root_id)
    if tree:
        store.update_thread(root_id, worktree_path=tree["path"], branch=tree["branch"], cwd=tree["path"])
    store.set_card(root_id, happened=f"Opened because {why}.")
    store.update_session(session["id"], thread_ts=root_id)
    stream.thread(store.thread(root_id))
    stream.session(store.session(session["id"]), "created")

    receipt = store.add_message(
        spec.chat_id, text=f"Opened **{args.get('title')}** — {why}.",
        author_kind="agent", author_id="project-coordinator",
    )
    stream.message(receipt)

    brief = store.add_message(spec.chat_id, text=str(args.get("brief") or ""), author_kind="owner",
                              thread_ts=root_id,
                              extra={"cited": {"message_id": root_id}})
    stream.message(brief)
    wake(store.session(session["id"]), brief)


def _message_thread(spec: TurnSpec, args: dict, why: str) -> None:
    """Relay the owner's words into a running thread. Copied, not retyped."""
    ts = str(args.get("thread_ts") or "")
    thread = store.thread(ts)
    if not thread:
        return
    relayed = store.add_message(
        thread["chat_id"], text=str(args.get("text") or ""), author_kind="owner", thread_ts=ts,
        extra={"relayed_from": {"chat_id": spec.chat_id, "message_id": (spec.message or {}).get("id")}},
    )
    stream.message(relayed)
    hop = store.add_hop(kind="relay", chat_id=thread["chat_id"], from_chat_id=spec.chat_id,
                        message_id=relayed["id"], source_message_id=(spec.message or {}).get("id"),
                        thread_ts=ts)
    stream.hop(hop)

    receipt = store.add_message(spec.chat_id, text=f"Passed to **{thread['title']}** — {why}.",
                                author_kind="agent", author_id="project-coordinator")
    stream.message(receipt)

    if thread.get("state") in ("needs_reply", "paused", "needs_approval"):
        row = store.update_thread(ts, state="working")
        if row:
            stream.thread(row)
    if thread.get("session_id"):
        wake(store.session(thread["session_id"]), relayed)


def _update_status(spec: TurnSpec, args: dict, why: str) -> None:
    if not spec.thread_ts:
        return
    row = store.update_thread(spec.thread_ts, status_md=str(args.get("status_md") or ""))
    if row:
        stream.thread(row)


def _spawn(spec: TurnSpec, args: dict, why: str) -> None:
    """A sub-agent. It does no work here; it records that a pass was made."""
    run = store.add_run(session_id=spec.session_id, title=str(args.get("title") or "Sub-agent"))
    stream.run_started(run)
    if spec.thread_ts:
        thread = store.thread(spec.thread_ts) or {}
        runs = dict(thread.get("runs") or {})
        runs["running"] = (runs.get("running") or 0) + 1
        runs["total"] = (runs.get("total") or 0) + 1
        row = store.update_thread(spec.thread_ts, runs=runs)
        if row:
            stream.thread(row)

    async def finish():
        await asyncio.sleep(max(1.0, config.TURN_DELAY_S * 2))
        store.update_run(run["id"], state="finished", status="ok", ended_at=ids.now_iso(),
                         summary=f"Looked over: {args.get('title')}.", tool_calls=3)
        stream.run_finished(store.run(run["id"]))
        if spec.thread_ts:
            thread = store.thread(spec.thread_ts) or {}
            runs = dict(thread.get("runs") or {})
            runs["running"] = max(0, (runs.get("running") or 1) - 1)
            row = store.update_thread(spec.thread_ts, runs=runs)
            if row:
                stream.thread(row)
        store.save()

    asyncio.get_event_loop().create_task(finish())


def _ask_owner(spec: TurnSpec, args: dict, why: str) -> None:
    """Open a decision. This is the one thing that stops a thread and waits."""
    item = store.add_review({
        "kind": str(args.get("kind") or "permission"),
        "title": str(args.get("title") or "It needs your decision"),
        "why": str(args.get("why") or why),
        "risk": str(args.get("risk") or "notable"),
        "risk_reasons": list(args.get("risk_reasons") or []),
        "blocking": bool(args.get("blocking", True)),
        "chat_id": spec.chat_id,
        "thread_ts": spec.thread_ts,
        "session_id": spec.session_id,
        "detail": dict(args.get("detail") or {}),
        "options": list(args.get("options") or []),
    })
    stream.review_opened(item)
    if spec.thread_ts:
        row = store.update_thread(spec.thread_ts, state="needs_approval")
        store.set_card(spec.thread_ts, needs_you=item["title"])
        if row:
            stream.thread(store.thread(spec.thread_ts))


def _finish(spec: TurnSpec, args: dict, why: str) -> None:
    if not spec.thread_ts:
        return
    summary = str(args.get("summary") or "Done.")
    row = store.update_thread(spec.thread_ts, state="done")
    store.set_card(spec.thread_ts, happened=summary, needs_you="")
    if row:
        stream.thread(store.thread(spec.thread_ts))
    note = store.add_message(spec.chat_id, text=summary, author_kind="agent",
                             author_id="thread-agent", thread_ts=spec.thread_ts)
    stream.message(note)


_HANDLERS = {
    "reply": _reply,
    "note": _note,
    "forward_to_project": _forward_to_project,
    "start_thread_session": _start_thread_session,
    "message_thread": _message_thread,
    "update_status": _update_status,
    "spawn": _spawn,
    "ask_owner": _ask_owner,
    "finish": _finish,
}


# ── the one way a message enters the system ──────────────────────────────────
def deliver_owner_message(chat_id: str, row: dict) -> None:
    """Route a message the owner just sent: to a thread's agent, or to the chat's coordinator."""
    if row.get("thread_ts"):
        thread = store.thread(row["thread_ts"])
        if thread and thread.get("session_id"):
            if thread.get("state") in ("needs_reply", "paused", "needs_approval"):
                updated = store.update_thread(row["thread_ts"], state="working")
                if updated:
                    stream.thread(updated)
            wake(store.session(thread["session_id"]), row)
        return

    chat = store.chat(chat_id)
    if not chat or (chat.get("routing") or {}).get("mode") == "off":
        return
    wake(store.coordinator_of(chat_id), row)
