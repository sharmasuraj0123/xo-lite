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
    """Markdown a project keeps for its coordinator: every *.md at its root."""
    project = store.project_of(chat)
    root = store.project_dir(project)
    if not root.is_dir():
        return {"files": []}
    files = []
    for path in sorted(root.glob("*.md")):
        try:
            files.append({"path": path.name, "bytes": path.stat().st_size})
        except OSError:
            continue
    return {"files": files}


@router.get("/chats/{chat}/memory/{path:path}")
async def get_memory_file(chat: str, path: str):
    """One of those files. Confined to the project root, like every other read."""
    project = store.project_of(chat)
    root = store.project_dir(project).resolve()
    try:
        target = (root / path).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        raise fail(400, "outside_project", "That path is outside the project.")
    if not target.is_file():
        raise fail(404, "not_found", "No such file.")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        raise fail(409, "unreadable", str(err))
    return {"path": path, "content": text, "bytes": len(text.encode("utf-8"))}


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


class SessionPatch(BaseModel):
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None


@router.patch("/sessions/{sid}")
async def patch_session(sid: str, body: SessionPatch):
    """The pin on a session. Only the four fields the client offers are writable."""
    row = store.session(sid)
    if not row:
        raise fail(404, "session_not_found", "No such session.")
    fields = body.model_dump(exclude_none=True)
    if fields:
        pin = dict(row.get("pin") or {})
        pin.update(fields)
        row = store.update_session(sid, pin=pin)
        store.save()
        stream.session(row, "patched")
    return row


def _stop_runs(rows: list[dict], why: str) -> int:
    """Mark running runs stopped and announce each one. Returns how many moved."""
    stopped = 0
    for run in rows:
        if run.get("state") not in ("running", "queued"):
            continue
        row = store.update_run(run["id"], state="finished", status="stopped",
                               ended_at=ids.now_iso(), summary=why)
        stream.run_finished(row)
        stopped += 1
    return stopped


@router.post("/runs/{rid}/stop")
async def stop_run(rid: str):
    row = store.run(rid)
    if not row:
        raise fail(404, "run_not_found", "No such run.")
    stopped = _stop_runs([row], "You stopped it.")
    store.save()
    return {"stopped": stopped}


@router.post("/sessions/{sid}/runs/stop")
async def stop_session_runs(sid: str):
    if not store.session(sid):
        raise fail(404, "session_not_found", "No such session.")
    stopped = _stop_runs(store.runs_of(sid).get("runs", []), "You stopped it.")
    row = store.update_session(sid, state="idle")
    if row:
        stream.session(row, "stopped")
    store.save()
    return {"stopped": stopped}


@router.post("/abort")
async def abort(body: dict = Body(default={})):
    """Stop a session where it stands.

    A thread caught mid-checklist is left `paused`, never `done`, so its card
    still reads as work someone has to pick back up.
    """
    sid = str(body.get("session_id") or "")
    session = store.session(sid)
    if not session:
        raise fail(404, "session_not_found", "No such session.")
    _stop_runs(store.runs_of(sid).get("runs", []), "You stopped it.")
    row = store.update_session(sid, state="idle")
    if row:
        stream.session(row, "aborted")
    ts = session.get("thread_ts")
    thread = store.thread(ts) if ts else None
    if thread and thread.get("state") == "working":
        store.update_thread(ts, state="paused")
        store.set_card(ts, happened="You stopped it.", needs_you="")
        stream.thread(store.thread(ts))
    store.save()
    return {"aborted": True, "session_id": sid}


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


def _option(item: dict, body: dict) -> dict | None:
    """The option the client picked, by id, else one matching a scope it named."""
    want = body.get("option") or body.get("option_id")
    for opt in item.get("options") or []:
        if want and opt.get("id") == want:
            return opt
    scope = body.get("scope")
    if scope:
        for opt in item.get("options") or []:
            if opt.get("scope") == scope:
                return opt
    return None


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

    # "Allow for this thread" / "Make it a rule": the client names one of the
    # item's own options, and the rule is that option's sentence, word for word.
    # Nothing is inferred - an option carrying no scope makes no rule.
    chosen = _option(item, body)
    made = None
    if decision in ("allow", "approve") and chosen and chosen.get("scope") in ("thread", "project", "rule"):
        scope = "space" if chosen["scope"] == "rule" else chosen["scope"]
        made = store.add_rule(sentence=chosen.get("sentence") or item["title"], scope=scope,
                              chat_id=item.get("chat_id"), thread_ts=item.get("thread_ts"),
                              tier=item.get("risk"), from_review=rid)

    closed = store.close_review(rid, decision, note)
    if made:
        closed["rule_id"] = made["id"]
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
    # No built-in defaults: every rule here was made by the owner answering a
    # decision, so there is nothing the server could have decided in advance.
    return {"rules": store.rules()[:limit], "defaults": [], "next_after_seq": None}


class NewRule(BaseModel):
    sentence: str
    scope: str = "thread"
    effect: str = "allow"
    chat_id: str | None = None
    thread_ts: str | None = None
    tier: str | None = None


@router.post("/rules")
async def post_rule(body: NewRule):
    if not body.sentence.strip():
        raise fail(422, "empty_rule", "A rule needs the sentence it permits.")
    row = store.add_rule(sentence=body.sentence, scope=body.scope, effect=body.effect,
                         chat_id=body.chat_id, thread_ts=body.thread_ts, tier=body.tier)
    store.save()
    return row


@router.delete("/rules/{rid}")
async def delete_rule(rid: str):
    row = store.revoke_rule(rid)
    if not row:
        raise fail(404, "rule_not_found", "That rule is already gone.")
    store.save()
    return {"revoked": True, "rule": row}


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
