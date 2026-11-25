"""Word handles in replies, for a connection that switched them on.

Off by default, so that no reply pays for them. ``profile(word_handles="on")``
switches them on for the connection the call came through (connection.py;
table ``connection_setting``, migration 0352). While on, the short id at the
start of a line gains the note's words (``4f9c21aa gentle beacon note ...``).

How many words: two, unless another note this reader may read has the same
two; then three; then four. Four words name all 28 bits a handle reads, so
past that the short id is what tells two notes apart. Which notes the reader
may read is ``readable_nodes`` alone, asked about the notes whose numbers
fall in the same range (``handle_number``, an indexed SQL function).

``server.run`` opens a call with :func:`during`; outside one, and while off,
:func:`words` is the empty string and nothing is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from reticula_mcp import connection, handle_words
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, ReticulaError
from reticula_mcp.permissions import readable_nodes_sql

SETTING = "word_handles"


@dataclass
class _Call:
    conn: Conn
    viewer: Account
    known: dict[str, str] = field(default_factory=dict)


_ACTIVE: ContextVar[_Call | None] = ContextVar("reticula_word_handles", default=None)


def is_on(conn: Conn) -> bool:
    """Whether the connection of this call has word handles on."""
    key = connection.current()
    if key is None:
        return False
    row = conn.execute(
        "SELECT value FROM connection_setting WHERE connection = %s AND name = %s",
        (key, SETTING),
    ).fetchone()
    return bool(row and row["value"] == "on")


def switch(conn: Conn, viewer: Account, value: str) -> str:
    """``profile(word_handles=...)``: on or off for this connection."""
    wanted = value.strip().lower()
    if wanted not in ("on", "off"):
        raise ReticulaError(BAD_INPUT, 'word_handles is "on" or "off"')
    key = connection.current()
    if key is None:
        raise ReticulaError(
            BAD_INPUT,
            "word handles are switched per connection, and this call has none",
        )
    conn.execute(
        "INSERT INTO connection_setting (connection, account_id, name, value)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (connection, name)"
        " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, viewer.id, SETTING, wanted),
    )
    if wanted == "on":
        return (
            "Word handles are on for this connection: a note's id at the start of"
            ' a line is followed by its words ("4f9c21aa gentle beacon"), and the'
            ' words work wherever an id does. profile(word_handles="off") turns'
            " them off."
        )
    return "Word handles are off for this connection."


@contextmanager
def during(conn: Conn, viewer: Account) -> Iterator[None]:
    """For one tool call: words in the replies when the connection has them on."""
    reset = _ACTIVE.set(_Call(conn, viewer) if is_on(conn) else None)
    try:
        yield
    finally:
        _ACTIVE.reset(reset)


def words(address: str) -> str:
    """The words of ``address`` for this reader, or "" when off."""
    call = _ACTIVE.get()
    if call is None:
        return ""
    if address not in call.known:
        try:
            call.known[address] = shortest(call.conn, call.viewer, address)
        except ValueError:  # an address without a hex tail has no words
            call.known[address] = ""
    return call.known[address]


def shortest(conn: Conn, viewer: Account, address: str) -> str:
    """The fewest words, from two, that no other note the reader may read
    shares with ``address``."""
    n = handle_words.number(address)
    low, high = handle_words.bounds(n >> (handle_words.BITS - 14), 2)
    in_range = (
        "handle_number(x.address) >= %(low)s AND handle_number(x.address) < %(high)s"
    )
    rows = conn.execute(
        f"SELECT x.address FROM node x WHERE {in_range} AND x.address <> %(address)s"
        f" AND x.id IN (SELECT v.node_id FROM ({readable_nodes_sql('%(viewer)s')}) v"
        f"   WHERE v.node_id = ANY (ARRAY(SELECT x.id FROM node x WHERE {in_range})))",
        {"viewer": viewer.id, "low": low, "high": high, "address": address},
    ).fetchall()
    others = [handle_words.number(r["address"]) for r in rows]
    for count in (2, 3, 4):
        shift = handle_words.BITS - 7 * count
        if not any(o >> shift == n >> shift for o in others):
            return handle_words.to_words(n, count)
    return handle_words.to_words(n, 4)
