"""Why a request was refused, said by the code that refused it, for the request
log (``requestlog``).

An answer in JSON carries its reason, and the log reads it from there. A page
does not: the sign-in page answers a wrong password with status 401 and HTML
for a person. The code that refuses says the reason here, in a fixed word of
its own (``refusal.note("signin: bad_credentials")``), and the log writes it.

Only fixed words. Never anything the caller sent: not an e-mail address, not a
handle, not a message that repeats one.

This module imports nothing of the package, so that any part of it can say a
reason (``requestlog`` itself imports the sign-in package).
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_BOX: ContextVar[dict[str, str] | None] = ContextVar("reticula_refusal", default=None)


def begin() -> tuple[dict[str, str], Token[dict[str, str] | None]]:
    """Open the box of one request. The request log calls this before the
    request is handled and reads ``box["reason"]`` after."""
    box: dict[str, str] = {}
    return box, _BOX.set(box)


def end(token: Token[dict[str, str] | None]) -> None:
    _BOX.reset(token)


def note(reason: str) -> None:
    """Say why the request at hand is refused. Outside a request (a test that
    calls a handler directly) it does nothing. A worker thread started for the
    request sees the same box: the context is copied, the box is not."""
    box = _BOX.get()
    if box is not None:
        box["reason"] = reason
