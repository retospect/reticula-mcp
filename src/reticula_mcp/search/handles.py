"""Word handles in replies, for a connection that switched them on.

Off by default, so that no reply pays for them.
``edit(ref="settings", text="word handles: on")`` switches them on for the
connection the call came through (settings.py, connection.py; table
``connection_setting``, migration 0352). While on, the short id at the
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
from reticula_mcp.permissions import readable_nodes_sql

SETTING = "word_handles"


@dataclass
class _Call:
    conn: Conn
    viewer: Account
    known: dict[str, str] = field(default_factory=dict)


_ACTIVE: ContextVar[_Call | None] = ContextVar("reticula_word_handles", default=None)


def is_on(conn: Conn) -> bool:
    """Whether the connection of this call has word handles on (settings.py:
    edit(ref="settings", text="word handles: on"))."""
    key = connection.current()
    if key is None:
        return False
    row = conn.execute(
        "SELECT value FROM connection_setting WHERE connection = %s AND name = %s",
        (key, SETTING),
    ).fetchone()
    return bool(row and row["value"] == "on")


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
