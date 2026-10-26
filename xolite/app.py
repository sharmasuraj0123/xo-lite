"""The app: three routers, one store, one stream.

Deliberately no CORS middleware. xo-space refuses browser writes whose Origin it
does not recognise, and that is the whole reason xo-coworker ships a proxy —
`serve.py` becomes the browser's origin, runs its own same-origin check, and
forwards without the browser's labels. Keeping that property means this server
is not something a page on another site can drive.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config, store
from .api import chat, files, projects
from .delegation import turns

log = logging.getLogger("xolite")

app = FastAPI(title="xo-lite", version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.include_router(chat.router)
app.include_router(files.router)
app.include_router(projects.router)


@app.on_event("startup")
async def startup() -> None:
    config.ensure_dirs()
    store.load()
    found = store.discover_projects()
    store.save()
    turns.engine()  # fail loudly here if XO_LITE_ENGINE names something that will not load
    log.info("xo-lite ready: %d project(s) under %s, engine=%s",
             len(found), config.PROJECTS_ROOT, config.ENGINE)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc: RequestValidationError):
    """FastAPI's 422 list, in the one error shape the client reads first."""
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(p) for p in (first.get("loc") or [])[1:])
    return JSONResponse(status_code=422, content={"detail": {
        "code": "invalid_request",
        "message": f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg", "invalid")),
    }})


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "engine": config.ENGINE,
        "projects_root": str(config.PROJECTS_ROOT),
        "home": str(config.HOME),
        "chats": len(store.STATE["chats"]),
        "threads": len(store.STATE["threads"]),
        "open_decisions": len(store.review("open")),
    }
