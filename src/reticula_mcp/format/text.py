"""Checks on free text that every tool takes, before it reaches the database."""

from __future__ import annotations

import re
from collections.abc import Iterable

from reticula_mcp.errors import BAD_INPUT, ReticulaError

# What PostgreSQL cannot store in text: NUL, and a surrogate code point on its
# own (a JSON escape such as \ud800 decodes to one; it has no UTF-8 form).
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def refuse_unstorable(**values: str | Iterable[str] | None) -> None:
    """Refuse a character that cannot be stored in any of the named values:
    NUL (U+0000) or a lone surrogate. No note, title, query or reference needs
    one, and without this check the call fails inside the database. A value
    may be a string, a list of strings, or None."""
    for name, value in values.items():
        texts = [value] if value is None or isinstance(value, str) else list(value)
        for text in texts:
            if not isinstance(text, str):
                continue
            found = _UNSTORABLE.search(text)
            if found:
                raise ReticulaError(
                    BAD_INPUT,
                    f"{name.replace('_', ' ')} contains the character"
                    f" U+{ord(found.group()):04X}, which cannot be stored",
                    hint="remove it and send the text again",
                )
