# xo-lite

**A small XO backend with real delegation and no model in the loop.**

It speaks the storm surface (`/api/chat/*`) that [xo-coworker](../xo-coworker)
talks to, does real work on real files, gives every thread its own git worktree —
and decides what happens to a message with rules rather than an LLM.

```sh
pip install -r requirements.txt
python run.py                     # http://127.0.0.1:5002
```

Then point the UI at it. Its default `XO_API` is already `http://127.0.0.1:5002`,
so there is nothing to configure:

```sh
cd ../xo-coworker && python serve.py     # :5180
```

## Why there is no model

In xo-space, a coordinator is an agent CLI that is heard **only through comms
tools** — `forward_to_project`, `start_thread_session`, `message_thread`. The
model's entire job is choosing which tool to call. Everything after the call is
server code: the copying, the worktree, the card, the audit trail.

So the model is not load-bearing, and this server makes that explicit. An engine
is handed a `TurnSpec` and returns `ToolCall`s:

```python
async def run_turn(spec: TurnSpec) -> TurnResult: ...
```

`xolite/engine/rules.py` is the one that ships. It decides by pattern, so the
same message always routes the same way and every call carries the reason it was
made. An LLM-backed engine would be another module with the same `run_turn` and
nothing else in the server would change — which is the point of the seam, not an
afterthought.

What an engine may **not** do matters as much: it never writes to the store,
never speaks as the owner, never invents who said what. It returns calls;
`xolite/delegation/turns.py` applies them. Same rule xo-space states as *who said
what is decided by the server*.

## What it actually does

Send "The atlas-web search page is slow. Profile the query path and then drop the
old index." to the workspace and, with no model anywhere:

1. The **workspace coordinator** reads `atlas-web` in the text and forwards it —
   a server-made copy with a `forwarded_from` receipt and a hop, never a retype.
2. The **project coordinator** sees an imperative and opens a thread, with its own
   session and its own git worktree on `xo/thread-<hex>`.
3. The **thread** splits the brief into a checklist, says the plan, and works
   forward — until it reaches *Drop the old index*, matches `drop`, and stops to
   ask. It names that step, not the first one.
4. You allow it. The thread resumes, finishes the list, and goes `done`.

Every one of those steps is visible in xo-coworker, because every one broadcasts a
stream frame.

## The tool vocabulary

| role | may call |
|---|---|
| `workspace-coordinator` | `forward_to_project` · `reply` · `ask_owner` |
| `project-coordinator` | `start_thread_session` · `message_thread` · `reply` · `ask_owner` |
| `thread` | `update_status` · `note` · `spawn` · `ask_owner` · `finish` · `reply` |

A call to a terminal tool ends the turn. A role calling a tool it does not own is
refused and logged — the engine cannot widen its own powers.

## Layout

| path | what it owns |
|---|---|
| `xolite/config.py` | every knob, read once |
| `xolite/store.py` | chats, messages, threads, sessions, review, hops, runs |
| `xolite/stream.py` | the one SSE stream, frame numbering and `Last-Event-ID` resume |
| `xolite/engine/base.py` | **the seam**: `TurnSpec`, `ToolCall`, `TurnResult` |
| `xolite/engine/rules.py` | the deterministic engine: routing, checklists, risk |
| `xolite/delegation/turns.py` | the turn loop, and the only code that applies a call |
| `xolite/delegation/worktree.py` | a git worktree per thread, removed only when clean |
| `xolite/api/chat.py` | `/api/chat/*` — the storm surface |
| `xolite/api/files.py` | `/api/files/*` — the filesystem, confined to the projects root |
| `xolite/api/projects.py` | project file and image reads, `/api/config/workspace` |

## Filesystem

Same paths and bodies as xo-space's `routers/cowork_agent/files.py`, so anything
written against that works here: `list-directory`, `content`, `content-binary`,
`save`, `mkdir`, `upload`, plus `delete`.

The difference is confinement. Every path is resolved — symlinks included — and
checked against the projects root before it is touched; one that escapes is
**refused**, not clamped. xo-space allows writes anywhere under `$HOME` and relies
on the UI's proxy to block the dangerous routes. This server blocks them itself,
so it is safe to run without that proxy in front of it.

## Storage

Everything is machine-local under `~/.xo-lite`, the same convention xo-space
follows. Nothing is written into a project except its git worktrees.

```
~/.xo-lite/
  state.json          the index: chats, threads, sessions, review, hops, runs
  messages/<chat>.jsonl   append-only, one file per chat
  frames.jsonl        every stream frame, for a Last-Event-ID resume
  attachments/        files on messages
  logs/<session>.log  every turn, every call, and the reason it was made
```

Delete the folder to start over. Projects themselves are untouched.

## Knobs

| var | default | meaning |
|---|---|---|
| `XO_LITE_PORT` | `5002` | port |
| `XO_LITE_PROJECTS` | `~/xo-projects` | every direct child folder is a project |
| `XO_LITE_HOME` | `~/.xo-lite` | the store |
| `XO_LITE_ENGINE` | `rules` | which engine decides |
| `XO_LITE_WORKTREES_ON` | `1` | give threads their own worktree |
| `XO_LITE_MAX_TURNS` | `4` | turns at once |
| `XO_LITE_TURN_DELAY` | `1.2` | a pause before a turn answers, so "Replying" is visible; `0` for tests |

## Writing another engine

Drop a module in `xolite/engine/` with `SUPPORTED_ROLES` and
`async run_turn(spec) -> TurnResult`, then set `XO_LITE_ENGINE` to its name. A bad
name fails at boot rather than on the first message.

The rules engine is the worked example, and its pure parts are worth reusing
whatever decides: `is_work`, `steps_of`, `risk_of`, `title_of`, `pick_project`.

## What it is not

It does not run an agent, write code, or execute anything from a message. A
thread keeps a checklist, says what it would do, and stops to ask when a step
looks dangerous. That is the half worth having without a model — the delegation,
the asking, and the record of who decided what.
