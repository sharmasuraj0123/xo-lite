"""The rules engine: decides by pattern, never by model.

Everything here is a pure function of the turn it is given, so the same message
always routes the same way and a routing choice can always be explained — every
call carries the reason it was made.

What it decides, by role:

  workspace-coordinator   which project a message is for
  project-coordinator     whether a message starts a thread, joins one, or is
                          answered in place
  thread                  what the work is, what needs the owner, when it is done

It is not pretending to be an agent. A thread does not write code; it keeps a
checklist, says what it would do, and stops to ask when a step looks dangerous.
That is the part worth having without a model: the delegation, the asking, and
the record of who decided what.
"""

from __future__ import annotations

import re

from .base import PROJECT, THREAD, WORKSPACE, ProjectFacts, ToolCall, TurnResult, TurnSpec

SUPPORTED_ROLES = [WORKSPACE, PROJECT, THREAD]

# ── words ────────────────────────────────────────────────────────────────────
STOP = {
    "the", "a", "an", "and", "or", "but", "to", "for", "of", "in", "on", "at", "by",
    "with", "from", "is", "are", "was", "were", "be", "been", "it", "its", "this",
    "that", "we", "i", "you", "our", "my", "me", "us", "can", "should", "would",
    "please", "then", "so", "if", "when", "now", "not", "no", "yes", "do", "does",
}

# A sentence that starts with one of these is asking for work, not making
# conversation. Being generous here costs little — the worst case is a thread
# opened for something that wanted an answer, which the owner can resolve in one
# click. Being stingy costs more: the message gets a shrug instead of work.
VERBS = {
    # change something
    "fix", "add", "write", "build", "make", "create", "update", "refactor", "rename",
    "move", "remove", "delete", "drop", "rewrite", "replace", "swap", "convert",
    "extract", "split", "merge", "revert", "restore", "clean", "tidy", "trim", "prune",
    "upgrade", "bump", "port", "migrate", "generate", "implement", "wire", "hook",
    # look into something
    "check", "investigate", "find", "audit", "review", "profile", "measure", "benchmark",
    "trace", "inspect", "debug", "diagnose", "compare", "validate", "verify", "confirm",
    "test", "reproduce", "triage",
    # run or ship something
    "run", "start", "stop", "deploy", "ship", "release", "publish", "cut", "tag",
    "install", "uninstall", "configure", "enable", "disable", "set", "switch", "turn",
    # make it better
    "optimise", "optimize", "improve", "speed", "harden", "secure", "cache", "scale",
    "reduce", "increase", "lower", "raise",
    # write it down
    "document", "draft", "sketch", "note", "log", "annotate", "instrument", "monitor",
    # move data about
    "sync", "pull", "push", "fetch", "backup", "archive", "index", "import", "export",
}

# What makes a step worth asking about. Order matters: the first match wins.
RISKS: list[tuple[str, str, str]] = [
    (r"\brm\s+-rf\b", "destructive", "deletes files and cannot be undone"),
    (r"\bDROP\s+(TABLE|INDEX|DATABASE|SCHEMA)\b", "destructive", "drops a live database object"),
    (r"\bTRUNCATE\b", "destructive", "empties a table"),
    (r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", "destructive", "deletes every row"),
    (r"\bgit\s+push\s+(--force|-f)\b", "destructive", "rewrites published history"),
    (r"\bgit\s+reset\s+--hard\b", "destructive", "discards uncommitted work"),
    (r"\bchmod\s+777\b", "destructive", "makes a path world-writable"),
    (r"\b(prod|production)\b", "notable", "touches production"),
    (r"\bdeploy|release|cut\s+a\s+release\b", "notable", "ships to people"),
    (r"\bmigrat(e|ion)\b", "notable", "changes a schema others read"),
    (r"\b(secret|credential|api[\s-]?key|token|\.env)\b", "notable", "touches secrets"),
    (r"\bsudo\b", "notable", "runs as another user"),
    (r"\b(drop|delete|remove|wipe|purge)\b", "notable", "removes something"),
]


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9_.-]*", str(text or "").lower()) if w not in STOP]


def mentions(text: str) -> list[str]:
    """Explicit `@project` targets, in the order they appear."""
    return [m.lower() for m in re.findall(r"@([A-Za-z0-9][\w.-]*)", str(text or ""))]


# ── routing ──────────────────────────────────────────────────────────────────
def score_project(text: str, facts: ProjectFacts) -> tuple[int, str]:
    """How well a project matches a message, and why.

    Three signals, strongest first: its own name, a word from one of its recent
    thread titles, and a shared word with an open thread.
    """
    said = set(words(text))
    if not said:
        return 0, ""

    name_parts = [p for p in re.split(r"[-_.]", facts.name.lower()) if p and p not in STOP]
    if facts.name.lower() in said:
        return 100, f"the message names {facts.name}"
    hit = [p for p in name_parts if p in said]
    if hit:
        return 60 + 10 * len(hit), f"“{hit[0]}” is part of {facts.name}"

    for title in facts.recent_titles:
        shared = said & set(words(title))
        if len(shared) >= 2:
            return 40 + len(shared), f"it reads like {facts.name}'s “{title}”"

    for thread in facts.open_threads:
        shared = said & set(words(thread.get("title", "")))
        if len(shared) >= 2:
            return 30 + len(shared), f"{facts.name} has an open thread about {', '.join(sorted(shared)[:2])}"
    return 0, ""


def pick_project(text: str, projects: list[ProjectFacts]) -> tuple[ProjectFacts | None, str]:
    """Which project a workspace message is for, and the reason."""
    named = mentions(text)
    if named:
        for want in named:
            for facts in projects:
                if facts.name.lower() == want:
                    return facts, f"you aimed it at @{facts.name}"
    best, best_score, best_why = None, 0, ""
    for facts in projects:
        score, why = score_project(text, facts)
        if score > best_score:
            best, best_score, best_why = facts, score, why
    return (best, best_why) if best_score >= 30 else (None, "")


def match_thread(text: str, threads: list[dict]) -> tuple[dict | None, str]:
    """An open thread this message is a follow-up to."""
    said = set(words(text))
    best, best_shared = None, 1
    for thread in threads:
        if thread.get("state") in ("resolved", "done"):
            continue
        shared = said & set(words(thread.get("title", "")))
        if len(shared) > best_shared:
            best, best_shared = thread, len(shared)
    if not best:
        return None, ""
    return best, f"it continues “{best['title']}”"


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()]


def is_work(text: str) -> bool:
    """Does this ask for something to be done, rather than something to be said?

    The imperative is rarely the first word of the message — "The totals are
    wrong. Fix the rounding" asks for work in its second sentence. So every
    sentence gets looked at, not just the opening one.
    """
    clean = str(text or "").strip().lstrip("@").strip()
    if not clean:
        return False
    for sentence in sentences(clean):
        if (words(sentence) or [""])[0] in VERBS:
            return True
    # "…and then drop the old index" — a second task joined onto the first
    joined = re.search(r"\b(?:and\s+then|then|also|after\s+that)\s+(\w+)", clean, re.I)
    if joined and joined.group(1).lower() in VERBS:
        return True
    if re.search(r"\b(please|can you|could you|i need|we need|make sure|let'?s)\b", clean.lower()):
        return True
    # a path or a filename is almost always work
    if re.search(r"[\w-]+/[\w./-]+|\w\.\w{2,4}\b", clean):
        return True
    return len(words(clean)) >= 12


# ── shaping the work ─────────────────────────────────────────────────────────
def title_of(text: str, limit: int = 72) -> str:
    """A thread title from the message: its first sentence, trimmed and tidied."""
    clean = re.sub(r"\s+", " ", re.sub(r"@[\w.-]+", "", str(text or ""))).strip()
    clean = re.split(r"(?<=[.!?])\s|\n", clean)[0].strip(" .,:;-")
    if not clean:
        return "New thread"
    if len(clean) > limit:
        clean = clean[: limit - 1].rsplit(" ", 1)[0] + "…"
    return clean[0].upper() + clean[1:]


def steps_of(text: str) -> list[str]:
    """The checklist a brief implies.

    Numbered or bulleted lines are taken as written. Otherwise the brief is split
    on sentences and on "then"/"and then", which is how people write two steps in
    one line.
    """
    raw = str(text or "").strip()
    bullets = [ln for ln in raw.splitlines() if re.match(r"^\s*(?:\d+[.)]|[-*•])\s+", ln)]
    if len(bullets) >= 2:
        return [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip() for ln in bullets][:8]
    parts = re.split(r"(?<=[.!?])\s+|\s*\b(?:and then|then)\b\s*", raw)
    steps = [re.sub(r"\s+", " ", p or "").strip(" .,:;-") for p in parts]
    steps = [s[0].upper() + s[1:] for s in steps if s and len(s) > 3][:8]
    return steps or [title_of(raw)]


def risk_of(text: str) -> tuple[str | None, list[str], str | None]:
    """The risk in a piece of text: its level, why, and the phrase that matched."""
    body = str(text or "")
    for pattern, level, reason in RISKS:
        found = re.search(pattern, body, re.I)
        if found:
            return level, [reason], found.group(0)
    return None, [], None


def checklist_md(steps: list[str], done: int = 0, doing: int = -1) -> str:
    """The status markdown a thread keeps: ✓ done, ✱ in progress, ○ to do."""
    out = []
    for i, step in enumerate(steps):
        mark = "✓" if i < done else ("✱" if i == (doing if doing >= 0 else done) else "○")
        out.append(f"{mark} {step}")
    return "\n".join(out)


def _counts(status_md: str) -> tuple[list[str], int]:
    steps, done = [], 0
    for line in str(status_md or "").splitlines():
        line = line.strip()
        if not line or line[0] not in "✓✱○◯✔":
            continue
        steps.append(line[1:].strip())
        if line[0] in "✓✔":
            done += 1
    return steps, done


# ── the turn ─────────────────────────────────────────────────────────────────
async def run_turn(spec: TurnSpec) -> TurnResult:
    if spec.role == WORKSPACE:
        return _workspace(spec)
    if spec.role == PROJECT:
        return _project(spec)
    if spec.role == THREAD:
        return _thread(spec)
    return TurnResult(ok=False, error=f"rules engine does not run the role {spec.role!r}")


def _workspace(spec: TurnSpec) -> TurnResult:
    text = spec.text
    facts, why = pick_project(text, spec.projects)
    if facts:
        return TurnResult(calls=[ToolCall("forward_to_project", {"project": facts.name}, why=why)])

    named = mentions(text)
    unknown = [n for n in named if not any(p.name.lower() == n for p in spec.projects)]
    if unknown:
        return TurnResult(calls=[ToolCall("ask_owner", {
            "kind": "create_project",
            "title": f"Start a project for {unknown[0]}?",
            "why": f"You aimed this at @{unknown[0]}, and no project by that name exists.",
            "risk": "notable",
            "detail": {"purpose": title_of(text), "name": unknown[0],
                       "why_new": "nothing here matched the name you used"},
            "options": [
                {"id": "o_allow", "decision": "allow", "label": "Create it"},
                {"id": "o_deny", "decision": "deny", "label": "Not now", "note": True},
            ],
        }, why=f"@{unknown[0]} is not a project yet")])

    if not spec.projects:
        return TurnResult(calls=[ToolCall("reply", {
            "text": "There are no projects yet. Make a folder under the projects root, or use "
                    "**Create XO Project**, and I will have somewhere to send this.",
        }, why="the workspace has no projects")])

    names = ", ".join(f"**{p.name}**" for p in spec.projects[:6])
    return TurnResult(calls=[ToolCall("reply", {
        "text": f"I could not tell which project this is for, so I have not sent it anywhere. "
                f"Aim it with `@project` and I will route it.\n\nYou have: {names}.",
    }, why="nothing matched well enough to route on")])


def _own(spec: TurnSpec) -> ProjectFacts | None:
    """This coordinator's own project, by chat id rather than by list order."""
    return next((p for p in spec.projects if p.chat_id == spec.chat_id), None)


def _project(spec: TurnSpec) -> TurnResult:
    text = spec.text
    facts = _own(spec)
    thread, why = match_thread(text, facts.open_threads if facts else [])
    if thread:
        return TurnResult(calls=[ToolCall("message_thread", {"thread_ts": thread["ts"], "text": text}, why=why)])

    if is_work(text):
        return TurnResult(calls=[ToolCall("start_thread_session", {
            "title": title_of(text),
            "brief": text,
            "label": _label_of(text),
        }, why="it asks for something to be done")])

    return TurnResult(calls=[ToolCall("reply", {
        "text": _answer(text, facts),
    }, why="it reads as a question, not a task")])


def _label_of(text: str) -> str | None:
    for label, pattern in (
        ("migration", r"\bmigrat"), ("a11y", r"\baccessib|\ba11y\b|\bkeyboard\b|\bscreen reader\b"),
        ("security", r"\bsecurity|\bauth|\bsecret|\btoken\b"), ("perf", r"\bperformance|\bslow\b|\bspeed\b"),
        ("docs", r"\bdocument|\breadme\b|\bdocs\b"), ("tests", r"\btest|\bcoverage\b"),
        ("infra", r"\bdeploy|\bdns\b|\bcert|\bbackup|\binfra"),
    ):
        if re.search(pattern, text, re.I):
            return label
    return None


def _answer(text: str, facts: ProjectFacts | None) -> str:
    """A plain answer in place, from what the store knows. It never guesses."""
    if not facts:
        return "I have nothing on this project yet."
    live = [t for t in facts.open_threads if t.get("state") not in ("resolved", "done")]
    asked_status = re.search(
        r"\b(status|what'?s|whats|where|how'?s|hows|anything|progress|going on|up to)\b", text, re.I)
    if asked_status:
        if not live:
            return f"Nothing is running in **{facts.name}** right now."
        rows = "\n".join(f"- **{t['title']}** — {t.get('state', 'working').replace('_', ' ')}" for t in live[:6])
        return f"**{facts.name}** has {len(live)} thread{'s' if len(live) != 1 else ''} open:\n\n{rows}"
    return (f"Noted, but I have not started anything — that did not read as a task. "
            f"Say it as one (“fix…”, “add…”, “check…”) and I will open a thread in **{facts.name}**.")


def _thread(spec: TurnSpec) -> TurnResult:
    """Read the brief once, then work forward until something needs the owner.

    The first turn and every later one take the same path on purpose. Judging the
    brief as a whole would name the wrong step: a brief ending "…and then drop the
    old index" is risky, but the risk belongs to step three, not to step one.
    """
    thread = spec.thread or {}
    steps, done = _counts(thread.get("status_md") or "")
    calls: list[ToolCall] = []

    if not steps:
        steps, done = steps_of(spec.text or thread.get("title", "")), 0
        calls.append(ToolCall("update_status", {"status_md": checklist_md(steps, done=0)},
                              why="the brief implies these steps"))
        calls.append(ToolCall("note", {"text": _plan_note(steps)}, why="say the plan before doing anything"))
        if len(steps) >= 3:
            calls.append(ToolCall("spawn", {"title": f"Check {steps[0][:40].lower()}"},
                                  why="the first step is worth its own pass"))

    calls.extend(_work_forward(steps, done, thread))
    return TurnResult(calls=calls)


def _work_forward(steps: list[str], done: int, thread: dict) -> list[ToolCall]:
    """Take steps until one needs the owner, or the checklist runs out.

    Stopping after a single step would leave the thread idle in the middle of its
    own list, which reads as stuck rather than busy.
    """
    calls: list[ToolCall] = []
    did: list[str] = []
    cursor = done
    while cursor < len(steps):
        step = steps[cursor]
        level, reasons, command = risk_of(step)
        if level:
            calls.append(ToolCall("update_status", {"status_md": checklist_md(steps, done=cursor, doing=cursor)},
                                  why=f"stopping at step {cursor + 1} of {len(steps)}"))
            if did:
                calls.append(ToolCall("note", {"text": _done_note(did, step)}, why="say what moved"))
            calls.append(_ask(level, reasons, command, step, thread))
            return calls
        did.append(step)
        cursor += 1

    calls.append(ToolCall("update_status", {"status_md": checklist_md(steps, done=len(steps))},
                          why="every step is done"))
    if did:
        calls.append(ToolCall("note", {"text": _done_note(did, None)}, why="say what moved"))
    calls.append(ToolCall("finish", {"summary": f"All {len(steps)} steps done."},
                          why="nothing is left on the checklist"))
    return calls


def _done_note(did: list[str], next_step: str | None) -> str:
    body = "Done:\n" + "\n".join(f"- {s}" for s in did) if len(did) > 1 else f"Done: **{did[0]}**."
    return body + (f"\n\nNext I need you: **{next_step}**" if next_step else "")


def _plan_note(steps: list[str]) -> str:
    rows = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    return f"Here is how I read this:\n\n{rows}\n\nI will take them in order and stop to ask before anything destructive."


def _ask(level: str, reasons: list[str], command: str | None, step: str, thread: dict) -> ToolCall:
    return ToolCall("ask_owner", {
        "kind": "permission",
        "title": step if len(step) <= 80 else step[:79] + "…",
        "why": f"This step {reasons[0]}." + (f" It matched “{command}”." if command else ""),
        "risk": level,
        "risk_reasons": reasons,
        "blocking": True,
        "detail": {"command": command, "text": f"From the brief of “{thread.get('title', 'this thread')}”."},
        "options": [
            {"id": "o_allow", "decision": "allow", "label": "Allow once"},
            {"id": "o_thread", "decision": "allow", "scope": "thread", "label": "Allow this in this thread"},
            {"id": "o_deny", "decision": "deny", "label": "Not now", "note": True},
        ],
    }, why=f"the step {reasons[0]}")
