"""Passwords that are refused because they are common.

``common_passwords.txt`` is the list "10k-most-common.txt" of SecLists
(https://github.com/danielmiessler/SecLists, path
Passwords/Common-Credentials/10k-most-common.txt, commit 913b32731749 of
2026-09-08), MIT License, Copyright (c) 2018 Daniel Miessler; the licence is
in ``common_passwords.LICENSE`` beside it. It is read once, case-folded.
"""

from __future__ import annotations

from functools import cache
from importlib import resources


@cache
def _common() -> frozenset[str]:
    text = (resources.files("reticula_mcp.auth") / "common_passwords.txt").read_text(
        encoding="utf-8"
    )
    return frozenset(
        line.strip().casefold() for line in text.splitlines() if line.strip()
    )


def is_common(password: str) -> bool:
    """Whether ``password`` is on the list of common passwords, ignoring case
    and surrounding spaces."""
    return password.strip().casefold() in _common()
