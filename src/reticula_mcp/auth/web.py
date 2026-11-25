"""Small HTTP helpers shared by the auth routes."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


async def read_limited(request: Request, limit: int) -> bytes | None:
    """The request body, or None when it is larger than ``limit`` bytes.

    Never buffers more than ``limit`` bytes: checks Content-Length first and
    counts the stream as it arrives.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def json_error(
    status: int,
    error: str,
    description: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "error_description": description}
    return JSONResponse(
        body, status_code=status, headers={**NO_STORE, **(headers or {})}
    )
