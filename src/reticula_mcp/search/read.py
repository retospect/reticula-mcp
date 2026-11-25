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
from typing import Any

from reticula_mcp import records
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import NOT_FOUND, ReticulaError
from reticula_mcp.permissions import current_nodes_sql, readable_nodes_sql
from reticula_mcp.resolve import resolve_ref
from reticula_mcp.search import render
from reticula_mcp.search.rows import fetch_rows, visible_subset

Row = dict[str, Any]

MEMBERS_SHOWN = 20
SUB_MEMBERS_SHOWN = 5
SUBHUBS_SHOWN = 30
LINKS_SHOWN = 30
MAX_DEPTH = 4
_SUFFIX_RE = re.compile(r"^[0-9a-f-]+$")


def read_ref(
    conn: Conn, viewer: Account, ref: str, *, history: bool, depth: int
) -> str:
    depth = max(1, min(int(depth), MAX_DEPTH))
    try:
        node = resolve_ref(conn, viewer, ref)
    except ReticulaError as exc:
        if exc.code == NOT_FOUND:
            gone = _withdrawn(conn, viewer, ref)
            if gone is not None:
                return _render_withdrawn(gone)
        raise
    row = fetch_rows(conn, viewer, [node["id"]], text_chars=None)[node["id"]]
    if row["kind"] == "hub":
        return _render_hub(conn, viewer, row, history=history, depth=depth)
    return _render_note(conn, viewer, row, history=history)


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


def _byline(conn: Conn, viewer: Account, row: Row) -> str:
    n = len(records.versions(conn, row["address"]))
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
    vs = records.versions(conn, address)
    lines = ["versions (oldest first):"]
    for i, v in enumerate(vs, 1):
        lines.append(f"  {i}. {v.version_hash} {render.stamp(v.created_at)} [{v.type}]")
    return lines


def _content(row: Row) -> list[str]:
    """The node's text, presented according to who wrote it (see ``render``)."""
    text = (row.get("text") or "").strip("\n")
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


def _render_note(conn: Conn, viewer: Account, row: Row, *, history: bool) -> str:
    ref = render.short_ref(row["address"])
    head = f"{ref} {row['kind']} {_title(row)}"
    tag = render.trust_tag(row)
    if tag:
        head += f" [{tag}]"
    lines = [head, f"address: {row['address']}", _byline(conn, viewer, row)]
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
    lines.extend(_content(row))
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


def _hub_body(
    conn: Conn,
    viewer: Account,
    hub: Row,
    *,
    history: bool,
    depth: int,
    indent: str,
    top: bool,
    seen: set[int],
) -> list[str]:
    params = {"hub": hub["id"], "person": viewer.person_id, "viewer": viewer.id}
    flt = _members_filter(history)
    sub_ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT n.id FROM node n WHERE n.kind = 'hub' AND {flt}"
            f" ORDER BY n.created_at DESC, n.id DESC LIMIT {SUBHUBS_SHOWN}",
            params,
        ).fetchall()
    ]
    lines: list[str] = []
    if sub_ids:
        counts = _counts(conn, viewer, sub_ids, history)
        subs = fetch_rows(conn, viewer, sub_ids, text_chars=100)
        lines.append(f"{indent}sub-hubs:")
        for sid in sub_ids:
            s = subs[sid]
            notes, hubs = counts.get(sid, (0, 0))
            size = f"{notes} notes" + (f", {hubs} hubs" if hubs else "")
            lines.append(
                f"{indent}- {render.short_ref(s['address'])} hub {_title(s)} ({size})"
            )
            if depth > 1 and sid not in seen:
                seen.add(sid)
                lines.extend(
                    _hub_body(
                        conn,
                        viewer,
                        s,
                        history=history,
                        depth=depth - 1,
                        indent=indent + "  ",
                        top=False,
                        seen=seen,
                    )
                )
    limit = MEMBERS_SHOWN if top else SUB_MEMBERS_SHOWN
    total_row = conn.execute(
        f"SELECT count(*) AS c FROM node n WHERE n.kind <> 'hub' AND {flt}", params
    ).fetchone()
    total = total_row["c"] if total_row else 0
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT n.id FROM node n WHERE n.kind <> 'hub' AND {flt}"
            f" ORDER BY n.created_at DESC, n.id DESC LIMIT {limit}",
            params,
        ).fetchall()
    ]
    if ids:
        rows = fetch_rows(conn, viewer, ids, text_chars=200)
        current = visible_subset(conn, viewer, ids, current=True)
        lines.append(f"{indent}members (newest first, {len(ids)} of {total}):")
        for i in ids:
            lines.append(
                indent
                + render.row_line(
                    rows[i],
                    viewer.person_id,
                    superseded=i not in current,
                    show_hub=False,
                )
            )
        if total > len(ids):
            ref = render.short_ref(hub["address"])
            lines.append(
                f"{indent}... {total - len(ids)} more; find them with search(query=..., hub={ref!r})"
            )
    elif not sub_ids:
        lines.append(f"{indent}(no members yet)")
    return lines


def _render_hub(
    conn: Conn, viewer: Account, hub: Row, *, history: bool, depth: int
) -> str:
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
    lines.extend(_content(hub))
    lines.extend(
        _hub_body(
            conn,
            viewer,
            hub,
            history=history,
            depth=depth,
            indent="",
            top=True,
            seen={hub["id"]},
        )
    )
    lines.append(
        f"next: put(text=..., hub={ref!r}) adds a note; history=true shows replaced notes"
    )
    return "\n".join(lines)
