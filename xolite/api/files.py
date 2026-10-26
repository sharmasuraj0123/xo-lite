"""Filesystem routes, confined to the projects root.

Same paths and bodies as xo-space's `routers/cowork_agent/files.py`, so anything
written against that works here. The difference is the confinement: every path
is resolved and checked against the projects root before it is touched, and a
path that escapes it is refused rather than clamped. xo-space allows writes
anywhere under $HOME and leans on the UI's proxy to refuse the dangerous routes;
this server refuses them itself, so it is safe to run without that proxy.
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from .. import config
from .errors import fail

router = APIRouter(prefix="/api/files", tags=["files"])

# Files larger than this are not read into a response.
MAX_READ = 8 * 1024 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".xo-worktrees"}


def resolve(raw: str, *, must_exist: bool = False) -> Path:
    """Turn a client path into a real one inside the projects root, or refuse it.

    Accepts an absolute path under the root or a path relative to it. Symlinks are
    resolved before the check, so a link out of the root is refused too.
    """
    root = config.PROJECTS_ROOT.resolve()
    text = str(raw or "").strip()
    if not text:
        raise fail(400, "bad_path", "A path is required.")
    candidate = Path(text).expanduser()
    full = (candidate if candidate.is_absolute() else root / candidate)
    try:
        full = full.resolve()
    except OSError as exc:
        raise fail(400, "bad_path", f"That path cannot be resolved ({type(exc).__name__}).")
    if full != root and root not in full.parents:
        raise fail(403, "outside_root", f"Paths must be inside {root}.")
    if must_exist and not full.exists():
        raise fail(404, "not_found", "No such file or folder.")
    return full


def _rel(path: Path) -> str:
    root = config.PROJECTS_ROOT.resolve()
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


class PathBody(BaseModel):
    path: str


class SaveBody(BaseModel):
    path: str
    content: str


class MkdirBody(BaseModel):
    path: str
    scaffold: bool = False
    display_name: str | None = None
    description: str | None = None


@router.post("/list-directory")
async def list_directory(body: PathBody):
    """One folder's children: name, kind, size, modified. Never recursive."""
    full = resolve(body.path, must_exist=True)
    if not full.is_dir():
        raise fail(400, "not_a_directory", "That path is a file.")
    entries = []
    for child in sorted(full.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in SKIP_DIRS:
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": _rel(child),
            "kind": "directory" if child.is_dir() else "file",
            "size_bytes": None if child.is_dir() else stat.st_size,
            "modified_at": int(stat.st_mtime),
        })
    return {"path": _rel(full), "entries": entries, "count": len(entries)}


@router.post("/content")
async def content(body: PathBody):
    """A text file. Binary content is refused here; use content-binary."""
    full = resolve(body.path, must_exist=True)
    if not full.is_file():
        raise fail(400, "not_a_file", "That path is a folder.")
    size = full.stat().st_size
    if size > MAX_READ:
        raise fail(413, "too_large", f"That file is {size} bytes; the limit for a read is {MAX_READ}.")
    try:
        text = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise fail(415, "not_text", "That file is not UTF-8 text. Use /api/files/content-binary.")
    return {"path": _rel(full), "name": full.name, "size_bytes": size, "truncated": False, "content": text}


@router.post("/content-binary")
async def content_binary(body: PathBody):
    """Any file, base64-encoded."""
    full = resolve(body.path, must_exist=True)
    if not full.is_file():
        raise fail(400, "not_a_file", "That path is a folder.")
    size = full.stat().st_size
    if size > MAX_READ:
        raise fail(413, "too_large", f"That file is {size} bytes; the limit for a read is {MAX_READ}.")
    return {
        "path": _rel(full), "name": full.name, "size_bytes": size,
        "content_base64": base64.b64encode(full.read_bytes()).decode("ascii"),
    }


@router.post("/save")
async def save(body: SaveBody):
    """Write a text file, making its parent folders. Atomic: a failed write leaves the old file."""
    full = resolve(body.path)
    if full.is_dir():
        raise fail(400, "not_a_file", "That path is a folder.")
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp = full.with_suffix(full.suffix + ".tmp")
    tmp.write_text(body.content, encoding="utf-8")
    tmp.replace(full)
    return {"path": _rel(full), "size_bytes": full.stat().st_size, "saved": True}


@router.post("/mkdir")
async def mkdir(body: MkdirBody):
    """Make a folder. With `scaffold`, make it a project: a git repo and a brief.

    This is what the UI's "Create XO Project" calls. The project chat is created
    by the projects scan on the next listing, so nothing here touches the store.
    """
    full = resolve(body.path)
    if full.exists():
        raise fail(409, "exists", f"{full.name} already exists.")
    full.mkdir(parents=True)
    made = [_rel(full)]
    if body.scaffold:
        name = body.display_name or full.name
        readme = full / "README.md"
        body_text = f"# {name}\n"
        if body.description:
            body_text += f"\n{body.description.strip()}\n"
        readme.write_text(body_text, encoding="utf-8")
        made.append(_rel(readme))
        xo = full / ".xo"
        xo.mkdir(exist_ok=True)
        (xo / "brief.md").write_text(
            f"# {name}\n\n{(body.description or 'No brief yet.').strip()}\n", encoding="utf-8")
        made.append(_rel(xo / "brief.md"))
        try:
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=str(full), capture_output=True, timeout=20)
        except (OSError, Exception):
            pass  # a project without git simply gets no worktrees
    return {"path": _rel(full), "project": full.name, "created": True, "files": made}


@router.post("/upload")
async def upload(file: UploadFile = File(...), path: str = Form(...)):
    """Put an uploaded file into a folder inside the projects root."""
    folder = resolve(path)
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise fail(400, "not_a_directory", "Uploads go into a folder.")
    name = Path(file.filename or "upload.bin").name
    target = folder / name
    size = 0
    with open(target, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                target.unlink(missing_ok=True)
                raise fail(413, "too_large", "A file may be at most 25 MB.")
            out.write(chunk)
    return {"path": _rel(target), "name": name, "size_bytes": size, "uploaded": True}


@router.post("/delete")
async def delete(body: PathBody):
    """Remove a file, or an empty folder. A folder with anything in it is refused."""
    full = resolve(body.path, must_exist=True)
    if full == config.PROJECTS_ROOT.resolve():
        raise fail(403, "refused", "The projects root is not deletable.")
    if full.is_dir():
        if any(full.iterdir()):
            raise fail(409, "not_empty", "That folder is not empty.")
        shutil.rmtree(full)
    else:
        full.unlink()
    return {"path": _rel(full), "deleted": True}
