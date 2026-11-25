"""Shared reference resolvers used by every tool handler. FROZEN."""

from __future__ import annotations

import re
from typing import Any

from reticula_mcp import format as fmt
from reticula_mcp import handle_words
from reticula_mcp.accounts import (
    Account,
    get_account_by_handle,
    get_account_by_person_id,
)
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, CONFLICT, NOT_FOUND, ReticulaError
from reticula_mcp.format.address import ADDRESS_SCHEME
from reticula_mcp.permissions import readable_among_sql

MIN_SUFFIX = 6
_SUFFIX_RE = re.compile(r"^[0-9a-f-]+$")
Row = dict[str, Any]


def resolve_ref(
    conn: Conn, viewer: Account, ref: str, *, allow_tombstoned: bool = False
) -> Row:
    """Resolve a note/hub reference to its ``node`` row.

    ``ref`` is a full address (``ret:<person>/<uuid>``, also accepted without
    the ``ret:``) or a unique suffix of the UUID (6+ hex characters) among
    nodes ``viewer`` can read. Raises
    ``BadInput`` for malformed refs, ``NotFound`` when nothing readable
    matches, ``Conflict`` (listing candidates) when a suffix is ambiguous.
    ``allow_tombstoned``: a FULL address may also resolve to a tombstoned
    node (so ``get`` can answer "deleted" instead of "not found").
    """
    ref = ref.strip()
    person, slash, _ = ref.partition("/")
    if slash and fmt.is_person_id(person):
        ref = ADDRESS_SCHEME + ref  # an address written without its scheme
    if ref.startswith(ADDRESS_SCHEME):
        try:
            fmt.parse_address(ref)
        except ValueError as exc:
            raise ReticulaError(BAD_INPUT, f"malformed address {ref!r}") from exc
        row = conn.execute(
            "SELECT n.* FROM node n WHERE n.address = %(ref)s AND n.id IN"
            f" ({readable_among_sql('SELECT a.id FROM node a WHERE a.address = %(ref)s')})",
            {"ref": ref, "viewer": viewer.id},
        ).fetchone()
        if row is None and allow_tombstoned:
            row = conn.execute(
                "SELECT n.* FROM node n WHERE n.address = %s AND n.tombstoned_at IS NOT NULL",
                (ref,),
            ).fetchone()
        if row is None:
            raise ReticulaError(NOT_FOUND, f"no readable note at {ref}")
        return row
    spoken = handle_words.to_prefix(ref)
    if spoken is not None:
        return _resolve_words(conn, viewer, ref, *spoken)
    suffix = ref.lower()
    if not _SUFFIX_RE.match(suffix) or len(suffix.replace("-", "")) < MIN_SUFFIX:
        raise ReticulaError(
            BAD_INPUT,
            f"cannot read {ref!r} as a reference",
            hint=f"use a full address (ret:...) or the last {MIN_SUFFIX}+ hex characters of its id",
        )
    rows = conn.execute(
        "SELECT n.* FROM node n"
        " WHERE n.id IN ("
        + readable_among_sql(
            "SELECT a.id FROM node a WHERE right(a.address, %(n)s) = %(suffix)s"
        )
        + ") AND right(n.address, %(n)s) = %(suffix)s ORDER BY n.id LIMIT 6",
        {"viewer": viewer.id, "n": len(suffix), "suffix": suffix},
    ).fetchall()
    if not rows:
        raise ReticulaError(NOT_FOUND, f"no readable note ends in {suffix!r}")
    if len(rows) > 1:
        # kind, title and text are other people's words: quoted, one line each
        from reticula_mcp.search import render  # here: search imports this module

        shown = "; ".join(
            f"{r['address']} ({render.kind_word(r['kind'])}:"
            f" {render.quote(r['title'] or r['text'], 40)})"
            for r in rows[:5]
        )
        raise ReticulaError(
            CONFLICT,
            f"{suffix!r} matches several notes: {shown}",
            hint="use more characters or the full address",
        )
    return rows[0]


def _resolve_words(
    conn: Conn, viewer: Account, ref: str, prefix: int, words: int
) -> Row:
    """A word handle (handle_words.py): two to four words name a range of the
    numbers ``handle_number`` reads from addresses. One readable note in it is
    the answer; several are a Conflict that names them with more words."""
    low, high = handle_words.bounds(prefix, words)
    in_range = (
        "handle_number(a.address) >= %(low)s AND handle_number(a.address) < %(high)s"
    )
    rows = conn.execute(
        "SELECT n.* FROM node n WHERE n.id IN ("
        + readable_among_sql(f"SELECT a.id FROM node a WHERE {in_range}")
        + ") ORDER BY n.id LIMIT 6",
        {"viewer": viewer.id, "low": low, "high": high},
    ).fetchall()
    said = " ".join(ref.lower().split())
    if not rows:
        raise ReticulaError(NOT_FOUND, f"no readable note is called {said!r}")
    if len(rows) > 1:
        from reticula_mcp.search import render  # here: search imports this module

        more = min(words + 1, 4)
        shown = "; ".join(
            f"{handle_words.to_words(handle_words.number(r['address']), more)}"
            f" = {render.short_ref(r['address'])} ({render.kind_word(r['kind'])}:"
            f" {render.quote(r['title'] or r['text'], 40)})"
            for r in rows[:5]
        )
        raise ReticulaError(
            CONFLICT,
            f"{said!r} names several notes: {shown}",
            hint="say the longer words, or use the id",
        )
    return rows[0]


def resolve_person(conn: Conn, ref: str) -> Account:
    """Resolve ``@handle`` or a person ID to an :class:`Account`."""
    ref = ref.strip()
    account: Account | None
    if ref.startswith("@"):
        account = get_account_by_handle(conn, ref)
    elif fmt.is_person_id(ref):
        account = get_account_by_person_id(conn, ref)
    else:
        raise ReticulaError(
            BAD_INPUT,
            f"cannot read {ref!r} as a person",
            hint="use @handle or a person ID (p...)",
        )
    if account is None or account.status == "disabled":
        raise ReticulaError(NOT_FOUND, f"no such person {ref!r}")
    return account
