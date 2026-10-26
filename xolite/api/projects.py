"""Project reads: where projects live, and one file or image out of one.

These are the routes the UI's file viewer calls. They are read-only on purpose —
writing goes through `/api/files/*`, which has the confinement checks in one
place rather than two.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from .. import config, store
from .errors import fail
from .files import MAX_READ, resolve

router = APIRouter(tags=["projects"])


@router.get("/api/config/workspace")
async def workspace_config():
    """Where projects live. The UI never hard-codes this."""
    root = str(config.PROJECTS_ROOT)
    return {"roots": {"default": root, "projects": root}, "default": root}


@router.get("/api/xo-projects/{project}/file")
async def project_file(project: str, relative_path: str = Query(...)):
    full = resolve(f"{project}/{relative_path}", must_exist=True)
    if not full.is_file():
        raise fail(400, "not_a_file", "That path is a folder.")
    size = full.stat().st_size
    truncated = size > MAX_READ
    try:
        text = full.read_text(encoding="utf-8")[:MAX_READ] if not truncated else \
            full.read_text(encoding="utf-8", errors="replace")[:MAX_READ]
    except UnicodeDecodeError:
        raise fail(415, "not_text", "That file is not text. Use the image route, or /api/files/content-binary.")
    return {
        "name": full.name, "kind": "file", "size_bytes": size,
        "truncated": truncated, "content": text,
    }


@router.get("/api/xo-projects/{project}/image")
async def project_image(project: str, relative_path: str = Query(...)):
    full = resolve(f"{project}/{relative_path}", must_exist=True)
    if not full.is_file():
        raise fail(400, "not_a_file", "That path is a folder.")
    kind = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
    if not kind.startswith("image/"):
        raise fail(415, "not_an_image", "That file is not an image.")
    return FileResponse(full, media_type=kind)


@router.get("/api/xo-projects")
async def list_projects():
    found = store.discover_projects()
    store.save()
    return {"root": str(config.PROJECTS_ROOT), "projects": found}


@router.get("/schedules")
@router.get("/api/schedules")
async def schedules():
    return {"schedules": []}
