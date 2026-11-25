"""Helpers for the tests of ``search(since=...)`` (no tests in this module).

Every time the listing reads comes from a signed record (``node.created_at``,
``node.updated_at``, the record of a filing, ``hub_grant.updated_at``), and the
record log takes no UPDATE. So a test cannot move a row into the past after the
fact: it writes the record with the past time, through :class:`Clock`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from reticula_mcp import format as fmt

HEADER = re.compile(r"(\d+) of (\d+), ")
OTHERS = re.compile(r"(\d+) of the (\d+) by other people")
MORE = re.compile(r"^\.\.\. (\d+) more", re.MULTILINE)


class Clock:
    """Sets the time that records signed inside ``with clock.ago(days=3):``
    carry. Outside such a block records carry the real time."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._moment: datetime | None = None
        real = fmt.format_created

        def created(moment: datetime | int | None = None) -> str:
            return real(self._moment if moment is None else moment)

        monkeypatch.setattr(fmt, "format_created", created)

    @contextmanager
    def ago(self, **delta: float) -> Iterator[datetime]:
        before = self._moment
        self._moment = datetime.now(UTC) - timedelta(**delta)
        try:
            yield self._moment
        finally:
            self._moment = before


def short(address: str) -> str:
    """The short id a listing prints at the start of a line."""
    return address[-8:]


def line_of(out: str, address: str) -> str:
    """The one result line of ``address``; fails when it has none or several."""
    found = [ln for ln in out.splitlines() if ln.startswith(f"- {short(address)} ")]
    assert len(found) == 1, (address, out)
    return found[0]


def listed(out: str) -> list[str]:
    """The short ids of the result lines, in the order printed."""
    return [ln.split()[1] for ln in out.splitlines() if ln.startswith("- ")]


def shown_and_total(out: str) -> tuple[int, int]:
    """``(shown, total)`` from the first line (``3 of 7, newest first``)."""
    found = HEADER.search(out.splitlines()[0])
    assert found, out
    return int(found.group(1)), int(found.group(2))


def by_others(out: str) -> int:
    """How many of the listed lines the first line says are by other people."""
    found = OTHERS.search(out.splitlines()[0])
    assert found, out
    return int(found.group(1))


def more(out: str) -> int:
    """The number in the ``... N more`` line, 0 when there is none."""
    found = MORE.search(out)
    return int(found.group(1)) if found else 0
