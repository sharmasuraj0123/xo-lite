"""One error shape, the one the client already understands.

`{"detail": {"code", "message"}}` — storm's shape, which xo-coworker's
`js/api.js` reads first. The `code` is what the UI words its message from, so it
is part of the contract, not a log line.
"""

from __future__ import annotations

from fastapi import HTTPException


def fail(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})
