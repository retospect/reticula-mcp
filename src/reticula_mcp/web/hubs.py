"""/hubs: the public hubs as nested lists, from ``public_nodes()`` (0354).

Each entry is a hub title or a note title and its short id. Nothing else is
read: no author, no handle, no text, no date, no count. An owner's private
sub-hub is the words "private hub": its title and address are not selected.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from reticula_mcp.db.pool import Conn
from reticula_mcp.web.html import e

SHORT = 8  # the short id that get and search show
UNTITLED = "(untitled note)"
UNTITLED_HUB = "(untitled hub)"
PRIVATE_HUB = "private hub"


@dataclass(frozen=True)
class Entry:
    id: int
    kind: str
    title: str | None
    address: str
    private: bool = False


def load(conn: Conn) -> tuple[list[int], dict[int, list[int]], dict[int, Entry]]:
    """The public hubs (roots), the children of each hub, and each entry."""
    edges = conn.execute(
        "SELECT node_id, parent_hub_id, private FROM public_nodes()"
    ).fetchall()
    private = {r["node_id"] for r in edges if r["private"]}
    # title and address only of what is not private
    ids = sorted({r["node_id"] for r in edges} - private)
    rows = conn.execute(
        "SELECT id, kind, title, address FROM node WHERE id = ANY(%s)", (ids,)
    ).fetchall()
    entries = {
        r["id"]: Entry(r["id"], r["kind"], r["title"], r["address"]) for r in rows
    }
    for i in private:
        entries[i] = Entry(i, "hub", None, "", private=True)
    roots: list[int] = []
    children: dict[int, list[int]] = defaultdict(list)
    for r in edges:
        if r["node_id"] not in entries:
            continue
        if r["parent_hub_id"] is None:
            roots.append(r["node_id"])
        elif r["node_id"] not in children[r["parent_hub_id"]]:
            children[r["parent_hub_id"]].append(r["node_id"])
    return roots, children, entries


def _key(entry: Entry) -> tuple[int, str, str]:
    # hubs first, then by title, then by address; private hubs after the others
    if entry.private:
        return (0, "\uffff", str(entry.id))
    return (entry.kind != "hub", (entry.title or "").lower(), entry.address)


def _label(entry: Entry) -> str:
    if entry.private:
        return f'<span class="private">{PRIVATE_HUB}</span>'
    title = (entry.title or "").strip() or (
        UNTITLED_HUB if entry.kind == "hub" else UNTITLED
    )
    return f'{e(title)} <span class="id">{e(entry.address[-SHORT:])}</span>'


def _items(
    ids: list[int],
    children: dict[int, list[int]],
    entries: dict[int, Entry],
    above: frozenset[int],
) -> list[str]:
    out = []
    for i in sorted(ids, key=lambda x: _key(entries[x])):
        entry = entries[i]
        below = [c for c in children.get(i, []) if c not in above]
        if entry.kind == "hub" and below:
            inner = "".join(_items(below, children, entries, above | {i}))
            out.append(
                f"<li><details><summary>{_label(entry)}</summary>"
                f"<ul>{inner}</ul></details></li>"
            )
        else:
            out.append(f"<li>{_label(entry)}</li>")
    return out


def body(conn: Conn) -> str:
    roots, children, entries = load(conn)
    intro = (
        "<h1>Public hubs</h1>\n"
        "<p>Hubs their owners made public, with the titles of the owner's own"
        " notes in them. Only titles and ids are shown. The text of a note is"
        " read only by the people its owner shared the hub with.</p>\n"
        '<p class="small">An owner makes a hub public with'
        " <code>share(hub=&quot;&lt;hub&gt;&quot;, person=&quot;public&quot;,"
        " mode=&quot;read&quot;)</code> and takes it off with"
        " <code>mode=&quot;none&quot;</code>.</p>\n"
    )
    if not roots:
        return intro + "<p>No hub is public on this server yet.</p>"
    items = "".join(_items(roots, children, entries, frozenset()))
    return intro + f'<ul class="tree">{items}</ul>'
