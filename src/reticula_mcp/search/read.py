"""The fisheye read behind ``get``: one node, its surroundings, nothing hidden
that the viewer may read, nothing revealed that they may not.

Rules this module keeps:

* the node itself comes from ``resolve_ref`` (``readable_nodes`` only);
* every neighbour (hub, link target, superseder, member, sub-hub, count) is
  filtered through ``readable_nodes`` before any of its fields are read; an
  outgoing link to something unreadable shows its address only, the same as for
  an address that does not exist;
* a withdrawn (tombstoned) atom reads as an address and nothing else, and only to
  people who could have seen it: its author, and readers of a hub its author
  filed it in;
* a hub's access list is shown in full only to its admin and to people who can
  add to it; a reader sees the owner and their own entry.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, NOT_FOUND, ReticulaError
from reticula_mcp.permissions import current_nodes_sql, readable_nodes_sql
from reticula_mcp.resolve import resolve_ref
from reticula_mcp.search import render
from reticula_mcp.search.rows import fetch_rows, visible_subset

Row = dict[str, Any]

MEMBERS_SHOWN = 20  # notes per page of a hub, first page and every next page
SUB_MEMBERS_SHOWN = 5  # notes shown for a sub-hub expanded inside a larger answer
SUBHUBS_SHOWN = 30
LINKS_SHOWN = 30
MAX_DEPTH = 4
TEXT_SLICE = 8_000  # characters of a note's text per answer
TEXT_BREAK_WINDOW = 400  # look this far back for a line break or a space to cut at
HUB_TEXT_SHOWN = 2_000  # characters of a hub's own text
HUB_TEXT_LINES = 25
HISTORY_SHOWN = 20  # newest versions listed with history=true
LINE_BUDGET = 120  # a hub answer stops expanding sub-hubs at about this many lines
RESERVED_LINES = 2  # kept free for the closing lines
MAX_OFFSET = 10**12
_SUFFIX_RE = re.compile(r"^[0-9a-f-]+$")
_DIGITS_RE = re.compile(r"^[0-9]+$")


def _split_offset(ref: str) -> tuple[str, int]:
    """``<ref>+<n>`` -> (``<ref>``, n). References never contain ``+``, so the
    suffix is a page offset: a member offset on a hub, a character offset on a
    note. Anything after the last ``+`` that is not a whole number is refused."""
    ref = ref.strip()
    base, plus, tail = ref.rpartition("+")
    if not plus:
        return ref, 0
    tail = tail.strip()
    if not _DIGITS_RE.fullmatch(tail):
        raise ReticulaError(
            BAD_INPUT,
            f"the offset after + must be a whole number of 0 or more, not {tail[:40]!r}",
            hint="get(ref='1a2b3c4d+20') reads from the 21st member or character",
        )
    return base.strip(), min(int(tail), MAX_OFFSET)


def read_ref(
    conn: Conn, viewer: Account, ref: str, *, history: bool, depth: int
) -> str:
    depth = max(1, min(int(depth), MAX_DEPTH))
    base, offset = _split_offset(ref)
    # The planner prices queries over ``current_nodes()`` above jit_above_cost
    # although they run in a few milliseconds; Postgres then spends ~60 ms per
    # query compiling them. A hub read runs dozens. ``SET LOCAL`` ends with the
    # caller's transaction.
    conn.execute("SET LOCAL jit = off")
    try:
        node = resolve_ref(conn, viewer, base)
    except ReticulaError as exc:
        if exc.code == NOT_FOUND:
            gone = _withdrawn(conn, viewer, base)
            if gone is not None:
                return _render_withdrawn(gone)
        raise
    row = fetch_rows(conn, viewer, [node["id"]], text_chars=None)[node["id"]]
    if row["kind"] == "hub":
        return _render_hub(
            conn, viewer, row, history=history, depth=depth, offset=offset
        )
    return _render_note(conn, viewer, row, history=history, offset=offset)


# --------------------------------------------------------------------- withdrawn


def _withdrawn(conn: Conn, viewer: Account, ref: str) -> Row | None:
    """A tombstoned node matching ``ref``, if the viewer is allowed to learn that
    it was withdrawn: its author, or someone who could have read it, which is
    the ``readable_nodes`` rule minus the tombstone (audience ``hub``, filed by
    its OWN author in a hub the viewer reads). A filing by anyone else only
    organises and tells nobody anything. Everything else stays
    indistinguishable from a missing address."""
    ref = ref.strip()
    if ref.startswith("ret:"):
        cond, arg = "n.address = %(ref)s", ref
    else:
        suffix = ref.lower()
        if not _SUFFIX_RE.match(suffix) or len(suffix.replace("-", "")) < 6:
            return None
        cond, arg = "right(n.address, length(%(ref)s)) = %(ref)s", suffix
    rows = conn.execute(
        f"""
        SELECT n.id, n.address FROM node n
        WHERE n.tombstoned_at IS NOT NULL AND {cond}
          AND (n.author = %(person)s
               OR (n.audience = 'hub' AND EXISTS (
                    SELECT 1 FROM membership m
                    WHERE m.node_id = n.id AND NOT m.withdrawn
                      AND m.audience = 'hub' AND m.author = n.author
                      AND m.hub_node_id IN ({readable_nodes_sql("%(viewer)s")}))))
        LIMIT 2
        """,
        {"ref": arg, "person": viewer.person_id, "viewer": viewer.id},
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _render_withdrawn(row: Row) -> str:
    return (
        f"{render.short_ref(row['address'])} withdrawn\n"
        f"address: {row['address']}\n"
        "This atom was withdrawn by its author; no content is kept."
    )


# ------------------------------------------------------------------------ shared


def _title(row: Row) -> str:
    return render.quote(render.label(row), 100)


def _version_count(conn: Conn, address: str) -> int:
    found = conn.execute(
        "SELECT count(*) AS n FROM record WHERE address = %s", (address,)
    ).fetchone()
    return int(found["n"]) if found else 0


def _byline(conn: Conn, viewer: Account, row: Row) -> str:
    n = _version_count(conn, row["address"])
    handle = f"@{row['author_handle']}" if row.get("author_handle") else row["author"]
    if row["author"] == viewer.person_id:
        handle += " (you)"
    line = f"by {handle} · created {render.stamp(row['created_at'])}"
    if n > 1:
        line += f" · edited {render.stamp(row['updated_at'])}"
    return line + f" · {n} version{'s' if n != 1 else ''}"


def _parents(conn: Conn, viewer: Account, node_id: int) -> list[Row]:
    """Hubs the node is filed in that the viewer can read, oldest filing first."""
    return conn.execute(
        f"""
        SELECT h.id, h.title, h.text, h.address
        FROM membership m JOIN node h ON h.id = m.hub_node_id
        WHERE m.node_id = %(node)s AND NOT m.withdrawn AND h.kind = 'hub'
          AND (m.audience = 'hub' OR m.author = %(person)s)
          AND h.id IN ({readable_nodes_sql("%(viewer)s")})
        ORDER BY m.id
        """,
        {"node": node_id, "person": viewer.person_id, "viewer": viewer.id},
    ).fetchall()


def _paths(conn: Conn, viewer: Account, node_id: int, own: Row | None = None) -> str:
    """``"Root" / "Sub" / "Hub"`` for up to three hubs the node is filed in
    (following the first readable parent upwards); ``own`` appends the node
    itself (for a hub's own path)."""
    paths: list[str] = []
    for parent in _parents(conn, viewer, node_id)[:3]:
        chain = [parent]
        seen = {node_id, parent["id"]}
        while len(chain) < 8:
            ups = [
                p for p in _parents(conn, viewer, chain[0]["id"]) if p["id"] not in seen
            ]
            if not ups:
                break
            chain.insert(0, ups[0])
            seen.add(ups[0]["id"])
        if own is not None:
            chain.append(own)
        paths.append(" / ".join(render.quote(render.label(c), 40) for c in chain))
    return "; ".join(paths)


def _chain_lines(conn: Conn, viewer: Account, node: Row) -> list[str]:
    """What this node supersedes, what supersedes it (readable ones only), and
    its typed links in both directions (unreadable targets as address stubs)."""
    lines: list[str] = []
    old = conn.execute(
        "SELECT o.id, o.address FROM supersedes s JOIN node o ON o.id = s.old_node_id"
        " WHERE s.new_node_id = %s ORDER BY o.id",
        (node["id"],),
    ).fetchall()
    new = conn.execute(
        "SELECT s.new_node_id AS id FROM supersedes s WHERE s.old_node_id = %s"
        " ORDER BY s.new_node_id",
        (node["id"],),
    ).fetchall()
    out = conn.execute(
        "SELECT l.rel, l.dst_address, d.id AS dst_id FROM link l"
        " LEFT JOIN node d ON d.address = l.dst_address"
        " WHERE l.src_node_id = %s ORDER BY l.rel, l.dst_address LIMIT %s",
        (node["id"], LINKS_SHOWN + 1),
    ).fetchall()
    inc = conn.execute(
        "SELECT DISTINCT l.rel, l.src_node_id AS id FROM link l"
        " WHERE (l.dst_node_id = %s OR l.dst_address = %s) AND l.src_node_id <> %s"
        " ORDER BY l.rel, l.src_node_id LIMIT %s",
        (node["id"], node["address"], node["id"], LINKS_SHOWN + 1),
    ).fetchall()
    ask = (
        [r["id"] for r in old]
        + [r["id"] for r in new]
        + [r["dst_id"] for r in out if r["dst_id"] is not None]
        + [r["id"] for r in inc]
    )
    readable = visible_subset(conn, viewer, ask)
    rows = fetch_rows(conn, viewer, sorted(readable), text_chars=200)

    def show(i: int) -> str:
        return f"{render.short_ref(rows[i]['address'])} {rows[i]['kind']} {_title(rows[i])}"

    if old:
        lines.append(
            "supersedes: "
            + "; ".join(
                show(r["id"]) if r["id"] in readable else r["address"] for r in old
            )
        )
    sup = [r["id"] for r in new if r["id"] in readable]
    if sup:
        lines.append("superseded by: " + "; ".join(show(i) for i in sup))
    shown_out = out[:LINKS_SHOWN]
    for r in shown_out:
        target = (
            show(r["dst_id"])
            if r["dst_id"] is not None and r["dst_id"] in readable
            else f"{r['dst_address']} (opaque)"
        )
        lines.append(f"link {r['rel']} -> {target}")
    shown_in = [r for r in inc if r["id"] in readable][:LINKS_SHOWN]
    for r in shown_in:
        lines.append(f"link {r['rel']} <- {show(r['id'])}")
    if len(out) > LINKS_SHOWN or len(inc) > LINKS_SHOWN:
        lines.append("(more links not shown)")
    return lines


def _history_lines(conn: Conn, address: str) -> list[str]:
    """The newest ``HISTORY_SHOWN`` versions, oldest of those first, numbered as
    in the whole history."""
    total = _version_count(conn, address)
    rows = conn.execute(
        "SELECT version_hash, created_at, type FROM record WHERE address = %s"
        " ORDER BY id DESC LIMIT %s",
        (address, HISTORY_SHOWN),
    ).fetchall()
    rows.reverse()
    if total > len(rows):
        lines = [f"versions (oldest first; newest {len(rows)} of {total}):"]
    else:
        lines = ["versions (oldest first):"]
    for i, v in enumerate(rows, total - len(rows) + 1):
        lines.append(
            f"  {i}. {v['version_hash']} {render.stamp(v['created_at'])} [{v['type']}]"
        )
    return lines


def _content(row: Row, text: str) -> list[str]:
    """``text`` (all of the node's text or one slice of it), presented according
    to who wrote it (see ``render``). The quoted block's nonce covers exactly the
    text that is shown."""
    text = text.strip("\n")
    if not text.strip():
        return []
    ref = render.short_ref(row["address"])
    if render.is_approved_instruction(row):
        return [
            f"[instruction from approved source @{row['author_handle']}]",
            text,
            "[end instruction]",
        ]
    author = f"@{row['author_handle']}" if row.get("author_handle") else row["author"]
    out = []
    if row["kind"] == "instruction":
        out.append(f"WARNING: instruction atom, {render.UNTRUSTED}.")
    out.append(render.quoted_block(text, author, ref, row["head_version"]))
    return out


# -------------------------------------------------------------------------- note


def _text_part(row: Row, offset: int) -> list[str]:
    """The note's text from character ``offset`` on, at most ``TEXT_SLICE``
    characters, cut at a line break or space when one is near; when the note has
    more, one line outside the quoted block says how much and what to call."""
    text = row.get("text") or ""
    total = len(text)
    if offset == 0 and total <= TEXT_SLICE:
        return _content(row, text)
    if offset >= total:
        return [f"text: no more; the note has {total} characters in all"]
    ref = render.short_ref(row["address"])
    end = render.cut_end(text, offset, TEXT_SLICE, TEXT_BREAK_WINDOW)
    lines = _content(row, text[offset:end])
    if end < total:
        lines.append(
            f"text: characters {offset + 1} to {end} of {total};"
            f" next part: get(ref='{ref}+{end}')"
        )
    else:
        lines.append(
            f"text: characters {offset + 1} to {total} of {total}, the last part"
        )
    return lines


def _render_note(
    conn: Conn, viewer: Account, row: Row, *, history: bool, offset: int
) -> str:
    ref = render.short_ref(row["address"])
    head = f"{ref} {row['kind']} {_title(row)}"
    tag = render.trust_tag(row)
    if tag:
        head += f" [{tag}]"
    lines = [head, f"address: {row['address']}"]
    if offset > 0:  # a later part of the text: the head and the text only
        lines.extend(_text_part(row, offset))
        return "\n".join(lines)
    lines.append(_byline(conn, viewer, row))
    if row.get("ext_key"):
        lines.append(f"ext: {render.quote(str(row['ext_key']), 80)}")
    if row["audience"] == "private":
        lines.append("private: only you can read this")
    filed = _paths(conn, viewer, row["id"])
    if filed:
        lines.append(f"in: {filed}")
    lines.extend(_chain_lines(conn, viewer, row))
    if row.get("tags"):
        lines.append("tags: " + " ".join(render.quote(t, 30) for t in row["tags"][:12]))
    if history:
        lines.extend(_history_lines(conn, row["address"]))
    lines.extend(_text_part(row, 0))
    if row["author"] == viewer.person_id:
        lines.append(
            f"next: edit(ref={ref!r}) to change it; link(ref={ref!r}, to=<hub>) to file it"
        )
    else:
        lines.append(f"next: link(ref={ref!r}, to=<hub>) files it under a hub")
    return "\n".join(lines)


# --------------------------------------------------------------------------- hub


def _visible_fn(history: bool) -> str:
    return (
        readable_nodes_sql("%(viewer)s") if history else current_nodes_sql("%(viewer)s")
    )


def _members_filter(history: bool) -> str:
    return f"""
        n.id IN (SELECT m.node_id FROM membership m
                 WHERE m.hub_node_id = %(hub)s AND NOT m.withdrawn
                   AND (m.audience = 'hub' OR m.author = %(person)s))
        AND n.tombstoned_at IS NULL
        AND n.id IN ({_visible_fn(history)})
    """


def _counts(
    conn: Conn, viewer: Account, hub_ids: list[int], history: bool
) -> dict[int, tuple[int, int]]:
    """(notes, hubs) per hub, counting only members the viewer can read."""
    if not hub_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT m.hub_node_id AS hub,
               count(DISTINCT n.id) FILTER (WHERE n.kind <> 'hub') AS notes,
               count(DISTINCT n.id) FILTER (WHERE n.kind = 'hub') AS hubs
        FROM membership m JOIN node n ON n.id = m.node_id
        WHERE m.hub_node_id = ANY(%(hubs)s) AND NOT m.withdrawn
          AND (m.audience = 'hub' OR m.author = %(person)s)
          AND n.tombstoned_at IS NULL
          AND n.id IN ({_visible_fn(history)})
        GROUP BY m.hub_node_id
        """,
        {"hubs": hub_ids, "person": viewer.person_id, "viewer": viewer.id},
    ).fetchall()
    return {r["hub"]: (r["notes"], r["hubs"]) for r in rows}


# the same sentence as the `share` listing (graph lane)
READERS_NOTE = "other readers are shown only to people who can add to this hub"


def _access_line(conn: Conn, viewer: Account, hub: Row) -> str | None:
    """Who has access to the hub, as far as this viewer may know.

    The hub's admin and everyone who can add to it see the whole list. A
    viewer who can only read sees the owner and their own entry: no other
    name and no count of the others, with the same closing remark whether or
    not there are others (every account reads the shipped instructions hub, so
    the full list there would name every handle on the instance).
    """
    mode_row = conn.execute(
        "SELECT hub_access(%s, %s) AS mode", (viewer.person_id, hub["id"])
    ).fetchone()
    mode = mode_row["mode"] if mode_row else None
    is_owner = hub["author"] == viewer.person_id
    if not is_owner and mode not in ("read", "add"):
        return None
    owner = f"@{hub['author_handle']}" if hub.get("author_handle") else hub["author"]
    parts = [f"{owner} owner"]
    if not is_owner and mode != "add":
        me = f"@{viewer.handle}" if viewer.handle else viewer.person_id
        return f"access: {owner} owner, {me} read (you); {READERS_NOTE}"
    grants = conn.execute(
        "SELECT g.grantee, g.mode, a.handle FROM hub_grant g"
        " LEFT JOIN account a ON a.person_id = g.grantee"
        " WHERE g.hub_node_id = %s AND g.mode IN ('read', 'add') ORDER BY g.id",
        (hub["id"],),
    ).fetchall()
    for g in grants:
        if g["grantee"] == hub["author"]:
            continue  # the owner is already named
        who = f"@{g['handle']}" if g["handle"] else g["grantee"]
        parts.append(f"{who} {g['mode']}")
    return "access: " + ", ".join(parts)


@dataclass
class _Sub:
    row: Row
    notes: int
    hubs: int
    body: _Body | None = None


@dataclass
class _Body:
    """What one hub shows: its sub-hubs (with counts) and one page of members."""

    hub: Row
    subs: list[_Sub]
    sub_total: int
    members: list[str]  # rendered member lines, no indent
    total: int  # members the viewer can read (notes, not hubs)
    offset: int

    @property
    def cost(self) -> int:
        """Lines ``_body_lines`` emits for this body, children not counted."""
        lines = 0
        if self.subs:
            lines += 1 + len(self.subs) + (self.sub_total > len(self.subs))
        if self.members:
            lines += 1 + len(self.members)
            lines += self.total > self.offset + len(self.members)
        elif not self.subs:
            lines += 1
        return lines


def _load_body(
    conn: Conn,
    viewer: Account,
    hub: Row,
    *,
    history: bool,
    limit: int,
    offset: int = 0,
    with_subs: bool = True,
) -> _Body:
    params = {"hub": hub["id"], "person": viewer.person_id, "viewer": viewer.id}
    flt = _members_filter(history)
    sub_ids: list[int] = []
    if with_subs:
        sub_ids = [
            r["id"]
            for r in conn.execute(
                f"SELECT n.id FROM node n WHERE n.kind = 'hub' AND {flt}"
                " ORDER BY n.created_at DESC, n.id DESC LIMIT %(cap)s",
                {**params, "cap": SUBHUBS_SHOWN},
            ).fetchall()
        ]
    counts = _counts(conn, viewer, [hub["id"], *sub_ids], history)
    total, sub_total = counts.get(hub["id"], (0, 0))
    rows = fetch_rows(conn, viewer, sub_ids, text_chars=100)
    subs = [_Sub(rows[i], *counts.get(i, (0, 0))) for i in sub_ids if i in rows]
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT n.id FROM node n WHERE n.kind <> 'hub' AND {flt}"
            " ORDER BY n.created_at DESC, n.id DESC LIMIT %(limit)s OFFSET %(off)s",
            {**params, "limit": limit, "off": offset},
        ).fetchall()
    ]
    members: list[str] = []
    if ids:
        mrows = fetch_rows(conn, viewer, ids, text_chars=200)
        current = visible_subset(conn, viewer, ids, current=True)
        members = [
            render.row_line(
                mrows[i], viewer.person_id, superseded=i not in current, show_hub=False
            )
            for i in ids
            if i in mrows
        ]
    return _Body(hub, subs, sub_total, members, total, offset)


def _call(ref: str, offset: int, history: bool) -> str:
    return f"get(ref='{ref}+{offset}'" + (", history=true)" if history else ")")


def _body_lines(body: _Body, indent: str, history: bool) -> list[str]:
    """Sub-hubs (each followed by its own expansion, when it has one), then the
    members. Exactly ``body.cost`` lines for this body."""
    ref = render.short_ref(body.hub["address"])
    out: list[str] = []
    if body.subs:
        out.append(f"{indent}sub-hubs:")
        for s in body.subs:
            size = f"{s.notes} notes" + (f", {s.hubs} hubs" if s.hubs else "")
            out.append(
                f"{indent}- {render.short_ref(s.row['address'])} hub"
                f" {_title(s.row)} ({size})"
            )
            if s.body is not None:
                out.extend(_body_lines(s.body, indent + "  ", history))
        if body.sub_total > len(body.subs):
            out.append(
                f"{indent}... {body.sub_total - len(body.subs)} more sub-hubs;"
                f" find them with search(query=..., kind='hub', hub={ref!r})"
            )
    if body.members:
        shown = len(body.members)
        out.append(f"{indent}members (newest first, {shown} of {body.total}):")
        out.extend(indent + line for line in body.members)
        if body.total > shown:
            out.append(
                f"{indent}... {body.total - shown} more;"
                f" next page: {_call(ref, shown, history)}"
            )
    elif not body.subs:
        out.append(f"{indent}(no members yet)")
    return out


def _expand(
    conn: Conn, viewer: Account, top: _Body, *, history: bool, depth: int, used: int
) -> list[_Sub]:
    """Expand sub-hubs breadth first down to ``depth`` while the answer stays
    within ``LINE_BUDGET`` lines. Returns the sub-hubs left unexpanded only
    because of the budget (empty when everything asked for was shown)."""
    seen = {top.hub["id"]}
    queue = deque((s, 1) for s in top.subs)
    skipped: list[_Sub] = []
    while queue:
        sub, level = queue.popleft()
        if level >= depth or sub.row["id"] in seen:
            continue
        seen.add(sub.row["id"])
        body = _load_body(
            conn, viewer, sub.row, history=history, limit=SUB_MEMBERS_SHOWN
        )
        if used + body.cost > LINE_BUDGET:
            skipped = [sub] + [s for s, lv in queue if lv < depth]
            break
        used += body.cost
        sub.body = body
        queue.extend((s, level + 1) for s in body.subs)
    return skipped


def _budget_line(skipped: list[_Sub]) -> str:
    first = render.short_ref(skipped[0].row["address"])
    return (
        f"not expanded, to keep this answer short: {len(skipped)} sub-hubs;"
        f" read one with get(ref=<a sub-hub>), for example get(ref='{first}')"
    )


def _cut_hub_text(row: Row) -> list[str]:
    text = str(row.get("text") or "")
    shown = text[:HUB_TEXT_SHOWN]
    shown = "\n".join(shown.split("\n")[:HUB_TEXT_LINES])
    lines = _content(row, shown)
    if len(shown) < len(text):
        lines.append(f"(hub text cut: {len(shown)} of {len(text)} characters shown)")
    return lines


def _render_hub_page(
    conn: Conn, viewer: Account, hub: Row, *, history: bool, offset: int
) -> str:
    """A page of members past the first: the head and address, then the list."""
    ref = render.short_ref(hub["address"])
    lines = [f"{ref} hub {_title(hub)}", f"address: {hub['address']}"]
    body = _load_body(
        conn,
        viewer,
        hub,
        history=history,
        limit=MEMBERS_SHOWN,
        offset=offset,
        with_subs=False,
    )
    shown = len(body.members)
    if not shown:
        lines.append(f"no more members: this hub has {body.total}")
        return "\n".join(lines)
    lines.append(
        f"members (newest first, {offset + 1} to {offset + shown} of {body.total}):"
    )
    lines.extend(body.members)
    rest = body.total - offset - shown
    if rest > 0:
        lines.append(
            f"... {rest} more; next page: {_call(ref, offset + shown, history)}"
        )
    else:
        lines.append(f"last page: {body.total} members in all")
    return "\n".join(lines)


def _render_hub(
    conn: Conn,
    viewer: Account,
    hub: Row,
    *,
    history: bool,
    depth: int,
    offset: int = 0,
) -> str:
    if offset > 0:
        return _render_hub_page(conn, viewer, hub, history=history, offset=offset)
    ref = render.short_ref(hub["address"])
    path = _paths(conn, viewer, hub["id"], own=hub)
    lines = [
        f"{ref} hub {_title(hub)}",
        f"address: {hub['address']}",
        _byline(conn, viewer, hub),
        f"path: {path or _title(hub)}",
    ]
    access = _access_line(conn, viewer, hub)
    if access:
        lines.append(access)
    if history:
        lines.extend(_history_lines(conn, hub["address"]))
    lines.extend(_cut_hub_text(hub))
    top = _load_body(conn, viewer, hub, history=history, limit=MEMBERS_SHOWN)
    used = sum(ln.count("\n") + 1 for ln in lines) + top.cost + RESERVED_LINES
    skipped = _expand(conn, viewer, top, history=history, depth=depth, used=used)
    lines.extend(_body_lines(top, "", history))
    if skipped:
        lines.append(_budget_line(skipped))
    lines.append(
        f"next: put(text=..., hub={ref!r}) adds a note; history=true shows replaced notes"
    )
    return "\n".join(lines)
