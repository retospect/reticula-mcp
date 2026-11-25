"""The one exception type tool handlers raise, and how it is rendered."""

from __future__ import annotations

NOT_FOUND = "NotFound"
FORBIDDEN = "Forbidden"
BAD_INPUT = "BadInput"
CONFLICT = "Conflict"
NOT_IMPLEMENTED = "NotImplemented"
INTERNAL = "Internal"

CODES = (NOT_FOUND, FORBIDDEN, BAD_INPUT, CONFLICT, NOT_IMPLEMENTED, INTERNAL)


class ReticulaError(Exception):
    """A failure the caller (an LLM) should see as text.

    ``code`` is one of :data:`CODES` (lanes may add more, CamelCase).
    Rendered as ``[error:<code>] <message>`` plus an optional ``hint:`` line.
    """

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.hint = hint

    def render(self) -> str:
        text = f"[error:{self.code}] {self.message}"
        if self.hint:
            text += f"\nhint: {self.hint}"
        return text


def not_implemented(lane: str, what: str) -> ReticulaError:
    """The body of every stub handler."""
    return ReticulaError(
        NOT_IMPLEMENTED,
        f"{what} is not implemented yet.",
        hint=f"owned by the {lane} lane",
    )
