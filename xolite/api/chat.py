"""The storm surface: `/api/chat/*`, what xo-coworker talks to.

Shapes follow the reference built from that client's own `js/api.js` and its
call sites. Two things matter more than the rest:

* **Every mutation broadcasts a frame.** The client never polls threads or
  messages — `watch()` and `watchThread()` are one-shot loads — so a write that
  announces nothing is invisible until a reload.
* **`before_seq` may be MAX_SAFE_INTEGER.** The client sends 9007199254740991 to
  mean "the newest page". It is a cursor, not a sentinel, and must not come back
  empty.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import config, ids, store, stream
from ..delegation import turns, worktree
from ..engine import rules
from .errors import fail

router = APIRouter(prefix="/api/chat", tags=["chat"])

ATTACHMENTS = config.HOME / "attachments"


# ── the engine, runtimes, router ─────────────────────────────────────────────
@router.get("/status")
async def status():
    """What the UI's dock line reads. `pools` empty means nothing is holding work back."""
    return {
        "engine": "running",
        "store": "ready",
        "stream": "running",
        "version": "xo-lite",
        "pools": list(store.STATE["pools"].values()),
        "review": {"open": len(store.review("open"))},
    }


@router.get("/runtimes")
async def runtimes():
    """One runtime: this server's own engine. It is signed in because it needs no account.

    `features` is what the UI asks before offering to stop a sub-agent; saying
    nothing supports stopping is honest here, since a rules turn is over in
    milliseconds.
    """
    return {"runtimes": [{
        "name": config.AGENT_NAME,
        "label": config.AGENT_LABEL,
        "installed": True,
        "error": None,
        "cli": {"logged_in": True, "account": "local", "plan": "local"},
        "models": [config.AGENT_MODEL],
        "features": {"features": {}},
    }]}


@router.get("/runtimes/{runtime}/models")
async def runtime_models(runtime: str):
    return {"models": []}


@router.get("/router")
async def get_router():
    return store.STATE["router"]


@router.put("/router")
async def put_router(body: dict = Body(default={})):
    allowed = {k: v for k, v in body.items() if k in ("mode", "model", "provider")}
    store.STATE["router"].update(allowed)
    store.save()
    stream.broadcast("settings", {"key": "router", "value": store.STATE["router"]})
    return store.STATE["router"]


@router.get("/router/models")
async def router_models():
    return {"models": []}


# ── chats ────────────────────────────────────────────────────────────────────
@router.get("/chats")
async def list_chats():
    store.discover_projects()
    store.save()
    return {"chats": store.chats()}


@router.get("/chats/{chat}")
async def get_chat(chat: str):
    row = store.chat(chat)
    if not row:
        raise fail(404, "chat_not_found", "No such chat.")
    return row


@router.patch("/chats/{chat}")
async def patch_chat(chat: str, body: dict = Body(default={})):
    row = store.chat(chat)
    if not row:
        raise fail(404, "chat_not_found", "No such chat.")
    for key in ("routing", "pin", "agent", "model", "effort"):
        if key in body:
            if key in ("agent", "model", "effort"):
                row["pin"] = {**(row.get("pin") or {}), key: body[key]}
            else:
                row[key] = body[key]
    store.save()
    stream.chat(row)
    return row


# ── messages ─────────────────────────────────────────────────────────────────
class PostMessage(BaseModel):
    text: str = ""
    client_id: str | None = None
    thread_ts: str | None = None
    attachments: list[str] | None = None


@router.get("/chats/{chat}/messages")
async def get_messages(
    chat: str,
    thread: str | None = None,
    before_seq: int | None = Query(default=None),
    after_seq: int | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=1000),
):
    if not store.chat(chat):
        raise fail(404, "chat_not_found", "No such chat.")
    return store.messages(chat, thread=thread, before_seq=before_seq, after_seq=after_seq, limit=limit)


@router.post("/chats/{chat}/messages")
async def post_message(chat: str, body: PostMessage):
    """The owner says something. Stored, announced, then handed to the router."""
    if not store.chat(chat):
        raise fail(404, "chat_not_found", "No such chat.")
    text = (body.text or "").strip()
    if not text and not body.attachments:
        raise fail(400, "empty", "A message needs text or a file.")

    extra = {}
    if body.attachments:
        extra["attachments"] = [_attachment_meta(a) for a in body.attachments]
    if body.client_id:
        extra["client_id"] = body.client_id

    row = store.add_message(chat, text=text, author_kind="owner", thread_ts=body.thread_ts, extra=extra)
    stream.message(row)
    store.save()
    turns.deliver_owner_message(chat, row)
    return row


# ── threads ──────────────────────────────────────────────────────────────────
@router.get("/chats/{chat}/threads")
async def get_threads(chat: str):
    return {"threads": store.threads_of(chat)}


@router.get("/chats/{chat}/threads/{ts}")
async def get_thread(chat: str, ts: str):
    row = store.thread(ts)
    if not row:
        raise fail(404, "thread_not_found", "No such thread.")
    return row


@router.patch("/chats/{chat}/threads/{ts}")
async def patch_thread(chat: str, ts: str, body: dict = Body(default={})):
    row = store.thread(ts)
    if not row:
        raise fail(404, "thread_not_found", "No such thread.")
    fields: dict = {}
    if "resolved" in body:
        fields["state"] = "resolved" if body["resolved"] else "working"
        fields["resolved_at"] = ids.now_iso() if body["resolved"] else None
    if body.get("paused") is False and row["state"] == "paused":
        fields["state"] = "working"
    for key in ("title", "label", "intelligence"):
        if key in body:
            fields[key] = body[key]
    updated = store.update_thread(ts, **fields)
    store.save()
    stream.thread(updated)
    return updated


@router.get("/chats/{chat}/hops")
async def get_hops(chat: str, thread: str | None = None,
                   before_seq: int | None = None, limit: int = 200):
    return {"hops": store.hops(chat, thread)[-limit:], "next_before_seq": None}


@router.get("/chats/{chat}/memory")
async def get_memory(chat: str):
    return {"files": []}


# ── attachments ──────────────────────────────────────────────────────────────
def _attachment_meta(att_id: str) -> dict:
    meta = ATTACHMENTS / f"{att_id}.json"
    if meta.exists():
        import json
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"id": att_id, "name": att_id, "type": "application/octet-stream", "bytes": None}


@router.post("/chats/{chat}/attachments")
async def post_attachment(chat: str, request: Request, name: str = Query(default="file")):
    if not store.chat(chat):
        raise fail(404, "chat_not_found", "No such chat.")
    data = await request.body()
    if len(data) > 25 * 1024 * 1024:
        raise fail(413, "too_large", "A file may be at most 25 MB.")
    ATTACHMENTS.mkdir(parents=True, exist_ok=True)
    att_id = f"att_{ids.token(16)}"
    (ATTACHMENTS / att_id).write_bytes(data)
    meta = {"id": att_id, "name": Path(name).name,
            "type": request.headers.get("content-type", "application/octet-stream").split(";")[0],
            "bytes": len(data)}
    import json
    (ATTACHMENTS / f"{att_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


@router.get("/chats/{chat}/attachments/{att_id}")
async def get_attachment(chat: str, att_id: str):
    safe = "".join(c for c in att_id if c.isalnum() or c in "_-")
    path = ATTACHMENTS / safe
    if not path.is_file():
        raise fail(404, "attachment_not_found", "No such file.")
    meta = _attachment_meta(safe)
    return FileResponse(path, media_type=meta.get("type") or "application/octet-stream",
                        filename=meta.get("name"))


# ── sessions and runs ────────────────────────────────────────────────────────
@router.get("/sessions")
async def get_sessions(role: str | None = None, after_seq: int | None = None,
                       before_seq: int | None = None, limit: int = 500):
    rows = store.sessions()
    if role:
        rows = [r for r in rows if r["role"] == role]
    if after_seq is not None:
        rows = [r for r in rows if (r.get("seq") or 0) > after_seq]
    return {"sessions": rows[:limit], "next_after_seq": None}


@router.get("/sessions/{sid}")
async def get_session(sid: str):
    row = store.session(sid)
    if not row:
        raise fail(404, "session_not_found", "No such session.")
    return row


@router.get("/sessions/{sid}/runs")
async def get_runs(sid: str, tree: str = "true", depth: int = 3, limit: int = 200):
    return store.runs_of(sid)


@router.get("/runs/{rid}")
async def get_run(rid: str):
    row = store.run(rid)
    if not row:
        raise fail(404, "run_not_found", "No such run.")
    return row


@router.get("/runs/{rid}/transcript")
async def get_transcript(rid: str, after: str | None = None, limit: int = 200):
    row = store.run(rid)
    if not row:
        raise fail(404, "run_not_found", "No such run.")
    items = [{"kind": "text", "text": f"**{row['title']}**", "at": row["started_at"]}]
    if row.get("summary"):
        items.append({"kind": "text", "text": row["summary"], "at": row.get("ended_at")})
    return {"items": items, "next_after": None}


@router.get("/active")
async def active():
    return {"sessions": [s for s in store.sessions() if s["state"] == "running"]}


@router.get("/events")
async def events(session: str | None = None, before_seq: int | None = None, limit: int = 500):
    return {"events": [], "next_after_seq": None}


# ── review ───────────────────────────────────────────────────────────────────
@router.get("/review")
async def get_review(state: str = "open", after_seq: int | None = None, limit: int = 500):
    return {"items": store.review(state)[:limit], "next_after_seq": None}


@router.get("/review/{rid}")
async def get_review_item(rid: str):
    item = store.review_item(rid)
    if not item:
        raise fail(404, "review_not_found", "That decision is no longer open.")
    return item


@router.post("/review/{rid}/decision")
async def decide(rid: str, body: dict = Body(default={})):
    """The owner answers. This is what lets a stopped thread carry on."""
    item = store.review_item(rid)
    if not item:
        raise fail(404, "review_not_found", "That decision is no longer open.")
    if item.get("state") != "open":
        raise fail(409, "already_decided", "That decision was already answered.")

    decision = str(body.get("decision") or "allow")
    note = body.get("note")
    closed = store.close_review(rid, decision, note)
    stream.review_decided(closed)

    ts = item.get("thread_ts")
    if ts and store.thread(ts):
        if decision in ("allow", "approve", "answer", "done", "continue"):
            _advance(ts)
            row = store.update_thread(ts, state="working")
            store.set_card(ts, needs_you="", happened=f"You allowed: {item['title']}")
            stream.thread(store.thread(ts))
            session = store.session(store.thread(ts).get("session_id") or "")
            if session:
                turns.wake(session, {"text": f"Allowed: {item['title']}", "id": None})
        else:
            store.update_thread(ts, state="paused")
            store.set_card(ts, needs_you="", happened=f"You said no to: {item['title']}"
                           + (f" — {note}" if note else ""))
            stream.thread(store.thread(ts))
    elif item.get("kind") == "create_project" and decision == "allow":
        name = str((item.get("detail") or {}).get("name") or body.get("answer") or "").strip()
        if name:
            folder = config.PROJECTS_ROOT / name
            folder.mkdir(parents=True, exist_ok=True)
            chat = store.ensure_chat(name)
            stream.chat(chat)
    store.save()
    return closed


# ── rules and worktrees ──────────────────────────────────────────────────────
@router.get("/rules")
async def get_rules(after_seq: int | None = None, limit: int = 500):
    return {"rules": store.STATE.get("rules", []), "next_after_seq": None}


@router.get("/worktrees")
async def get_worktrees(chat: str | None = None):
    trees = []
    for thread in store.STATE["threads"].values():
        if not thread.get("worktree_path"):
            continue
        if chat and thread["chat_id"] != chat:
            continue
        trees.append({"chat": thread["chat_id"], "ts": thread["ts"], "path": thread["worktree_path"],
                      "branch": thread.get("branch"), "title": thread.get("title")})
    return {"worktrees": trees, "reported": []}


@router.delete("/worktrees/{chat}/{ts}")
async def remove_worktree(chat: str, ts: str, confirm: str | None = None):
    thread = store.thread(ts)
    if not thread or not thread.get("worktree_path"):
        raise fail(404, "not_found", "That thread has no worktree.")
    project = store.project_of(thread["chat_id"])
    ok, why = worktree.remove(store.project_dir(project), thread["worktree_path"])
    if not ok:
        raise fail(409, "not_clean", why)
    store.update_thread(ts, worktree_path=None, branch=None)
    store.save()
    stream.thread(store.thread(ts))
    return {"removed": True}


# ── the stream ───────────────────────────────────────────────────────────────
@router.get("/stream")
async def chat_stream(request: Request):
    last = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    return StreamingResponse(
        stream.sse(last),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ── helpers ──────────────────────────────────────────────────────────────────
def _advance(ts: str) -> None:
    """Mark the step the owner just allowed as done, so the thread does not ask again."""
    thread = store.thread(ts) or {}
    steps, done = rules._counts(thread.get("status_md") or "")
    if steps and done < len(steps):
        store.update_thread(ts, status_md=rules.checklist_md(steps, done=done + 1, doing=done + 1))
