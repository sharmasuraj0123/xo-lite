"""A git worktree per thread, so two threads in one project never fight.

`<project>/.xo-worktrees/thread-<ts>` on branch `xo/thread-<hex>`, the same shape
xo-space uses. A project that is not a git repository simply does not get one —
the thread then works in the project folder itself, and says so.

Nothing here is destructive on its own: a worktree is removed only when git says
it is clean, and never when it holds uncommitted work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config, ids


def _git(args: list[str], cwd: Path, timeout: float = 20) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def is_repo(path: Path) -> bool:
    code, _ = _git(["rev-parse", "--is-inside-work-tree"], path)
    return code == 0


def create(project_dir: Path, thread_ts: str) -> dict | None:
    """Make this thread's worktree. Returns {path, branch} or None if there is no repo."""
    if not config.WORKTREES_ENABLED or not project_dir.is_dir() or not is_repo(project_dir):
        return None

    branch = f"xo/thread-{ids.token(8)}"
    holder = project_dir / config.WORKTREES_SUBDIR
    path = holder / f"thread-{thread_ts.replace('.', '-')}"
    holder.mkdir(parents=True, exist_ok=True)

    # Keep the worktree folder out of the project's own history.
    exclude = project_dir / ".git" / "info" / "exclude"
    try:
        if exclude.parent.is_dir():
            body = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if config.WORKTREES_SUBDIR not in body:
                exclude.write_text(body.rstrip("\n") + f"\n{config.WORKTREES_SUBDIR}/\n", encoding="utf-8")
    except OSError:
        pass

    code, out = _git(["worktree", "add", "-b", branch, str(path)], project_dir, timeout=60)
    if code != 0:
        # An unborn repository has nothing to branch from; that is not a failure
        # worth surfacing — the thread works in the project folder instead.
        return None
    return {"path": str(path), "branch": branch}


def remove(project_dir: Path, worktree_path: str) -> tuple[bool, str]:
    """Remove a worktree, but only when it is clean."""
    path = Path(worktree_path)
    if not path.exists():
        return True, "already gone"
    code, out = _git(["status", "--porcelain"], path)
    if code == 0 and out.strip():
        return False, "it still holds uncommitted changes"
    code, out = _git(["worktree", "remove", str(path)], project_dir, timeout=60)
    return code == 0, out.strip() or "removed"


def list_for(project_dir: Path) -> list[dict]:
    code, out = _git(["worktree", "list", "--porcelain"], project_dir)
    if code != 0:
        return []
    trees, current = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                trees.append(current)
            current = {"path": line[9:].strip()}
        elif line.startswith("branch "):
            current["branch"] = line[7:].strip().replace("refs/heads/", "")
    if current:
        trees.append(current)
    return [t for t in trees if config.WORKTREES_SUBDIR in t.get("path", "")]
