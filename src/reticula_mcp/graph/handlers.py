"""Tool handlers: put, edit, link, delete, share. OWNED BY the graph lane.

Thin wrappers over :mod:`reticula_mcp.graph.api`: resolve references, check the
caller's rights for a helpful error, call the API, format a short answer.
Signatures mirror the tool signatures in ``server.py`` and are frozen.
"""

from __future__ import annotations

from typing import Any

from reticula_mcp.accounts import Account, get_account_by_person_id
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import (
    BAD_INPUT,
    FORBIDDEN,
    NOT_FOUND,
    ReticulaError,
)
from reticula_mcp.graph import access, api
from reticula_mcp.permissions import readable_nodes_sql
from reticula_mcp.resolve import resolve_person, resolve_ref

Row = dict[str, Any]

# Tool-level hub presets -> payload members (None removes the member).
PRESETS: dict[str, dict[str, Any]] = {
    "group": {"preset": "group", "invite": "members"},
    "moderated": {"preset": "group", "invite": "admins"},
    "personal": {"preset": "personal", "invite": None},
}


def _label(row: Row) -> str:
    lines = (row["text"] or "").strip().splitlines()
    name = (row["title"] or (lines[0] if lines else ""))[:50]
    return f"{row['kind']} {row['address']}" + (f" ({name!r})" if name else "")


def _preset_payload(preset: str) -> dict[str, Any]:
    key = preset.strip().lower()
    if key not in PRESETS:
        raise ReticulaError(
            BAD_INPUT,
            f"unknown preset {preset!r}",
            hint="use group, moderated or personal",
        )
    return dict(PRESETS[key])


def _resolve_hub(conn: Conn, viewer: Account, ref: str, *, adding: bool) -> Row:
    """A readable hub; when ``adding`` also a hub that accepts adds from anyone
    (found by its full address although the caller cannot read it)."""
    try:
        row = resolve_ref(conn, viewer, ref)
    except ReticulaError as exc:
        if adding and exc.code == NOT_FOUND and ref.strip().startswith("ret:"):
            open_hub = conn.execute(
                "SELECT * FROM node WHERE address = %s AND kind = 'hub'"
                " AND tombstoned_at IS NULL AND audience = 'hub'"
                " AND payload->>'accept' = 'anyone'",
                (ref.strip(),),
            ).fetchone()
            if open_hub is not None:
                return open_hub
        raise
    if row["kind"] != "hub":
        raise ReticulaError(
            BAD_INPUT,
            f"{row['address']} is a {row['kind']}, not a hub",
            hint="create hubs with put(kind='hub', title=...)",
        )
    return row


def find_debug_hub(conn: Conn) -> Row | None:
    """The instance debug hub: authored by the system account, tagged
    ``slug:hub-debug``. Ordinary accounts cannot read it, so it is found by SQL."""
    return conn.execute(
        "SELECT n.* FROM node n JOIN account a ON a.person_id = n.author"
        " WHERE a.status = 'system' AND n.kind = 'hub'"
        " AND n.tombstoned_at IS NULL AND 'slug:hub-debug' = ANY(n.tags)"
        " ORDER BY n.id LIMIT 1"
    ).fetchone()


def _require_can_add(conn: Conn, viewer: Account, hub: Row) -> None:
    if access.can_add_person(conn, viewer.person_id, hub["id"]):
        return
    raise ReticulaError(
        FORBIDDEN,
        f"you can read {hub['address']} but not add to it",
        hint="ask the hub's admin or a member who can add for add access",
    )


def _hubs_of_superseded(conn: Conn, viewer: Account, node: Row) -> list[Row]:
    """Hubs the notes ``node`` supersedes are filed in, that the viewer can read
    and add to."""
    addresses = [
        a for a in (node["payload"] or {}).get("supersedes", []) if isinstance(a, str)
    ]
    rows = conn.execute(
        "SELECT DISTINCT h.id, h.address FROM node old"
        " JOIN membership m ON m.node_id = old.id AND NOT m.withdrawn"
        " JOIN node h ON h.id = m.hub_node_id"
        " WHERE old.address = ANY(%s) AND h.kind = 'hub' AND h.tombstoned_at IS NULL"
        f" AND h.id IN ({readable_nodes_sql('%s')}) ORDER BY h.id",
        (addresses, viewer.id),
    ).fetchall()
    return [r for r in rows if access.can_add_person(conn, viewer.person_id, r["id"])]


def _paper_next(node: Row, ext_id: str | None) -> str:
    """The hint for writing notes about a paper, with its real identifier."""
    ident = node.get("ext_key") or ext_id or "<the same id>"
    return (
        f"write notes about it with put(text=..., ext_id={ident},"
        " anchor=<first words of the passage>)"
    )


def put(
    conn: Conn,
    viewer: Account,
    *,
    text: str = "",
    kind: str = "note",
    hub: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    private: bool = False,
    supersedes: list[str] | None = None,
    ext_id: str | None = None,
    anchor: str | None = None,
    preset: str | None = None,
    model: str | None = None,
) -> str:
    kind = (kind or "note").strip().lower()
    extra: dict[str, Any] | None = None
    if kind == "hub":
        extra = _preset_payload(preset or "group")
    elif preset is not None:
        raise ReticulaError(BAD_INPUT, "preset only applies to kind='hub'")
    hub_row: Row | None = None
    debug_unfiled = False
    if hub is not None:
        hub_row = _resolve_hub(conn, viewer, hub, adding=True)
        _require_can_add(conn, viewer, hub_row)
    elif kind == "debug":
        hub_row = find_debug_hub(conn)
        debug_unfiled = hub_row is None
    node = api.create_atom(
        conn,
        viewer,
        kind=kind,
        text=text,
        title=title,
        tags=tags,
        hub_node_id=hub_row["id"] if hub_row else None,
        private=private,
        supersedes=supersedes or (),
        ext_id=ext_id,
        anchor=anchor,
        model=model,
        payload_extra=extra,
    )
    if kind == "paper" and node["author"] != viewer.person_id:
        return (
            f"paper already known: {node['address']}\nnext: {_paper_next(node, ext_id)}"
        )
    if kind == "debug" and hub is None and hub_row is not None:
        # filed by the server, not chosen by the assistant: say so plainly
        if node["audience"] == "private":
            return (
                f"saved debug note {node['address']}"
                " (private: only you can read it, the operator does not)"
            )
        return (
            f"saved debug note {node['address']}"
            " (filed for the operator; only the operator reads it)"
        )
    where = f" in hub {hub_row['address']}" if hub_row else ""
    if debug_unfiled:
        where = " (unfiled: this instance has no debug hub yet)"
    if node["audience"] == "private":
        where += " (private)"
    nxt = (
        "next: share(hub=<this address>, person='@handle') to invite someone"
        if kind == "hub"
        else "next: link(ref, to=<hub>) to file it under a hub"
        if hub_row is None and node["audience"] != "private"
        else "next: search or get to find it again"
    )
    if hub_row is None and node["audience"] != "private" and supersedes:
        # a correction nobody else can see until it is filed where the original is
        homes = _hubs_of_superseded(conn, viewer, node)
        if homes:
            calls = " or ".join(
                f"link(ref={node['address'][-8:]}, to={h['address'][-8:]})"
                for h in homes[:2]
            )
            nxt = (
                f"next: only you can read this until it is filed where the original"
                f" is: {calls}"
            )
    if kind == "paper":
        nxt = f"next: {_paper_next(node, ext_id)}\n{nxt}"
    return f"saved {node['kind']} {node['address']}{where}\n{nxt}"


def edit(
    conn: Conn,
    viewer: Account,
    *,
    ref: str,
    text: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    private: bool | None = None,
    preset: str | None = None,
) -> str:
    node = resolve_ref(conn, viewer, ref)
    if node["author"] != viewer.person_id:
        raise ReticulaError(
            FORBIDDEN,
            "only the author can edit a note",
            hint="write your own note and link it, or ask the author",
        )
    extra: dict[str, Any] | None = None
    if preset is not None:
        if node["kind"] != "hub":
            raise ReticulaError(BAD_INPUT, "preset only applies to hubs")
        extra = _preset_payload(preset)
    if all(v is None for v in (text, title, tags, private, extra)):
        raise ReticulaError(BAD_INPUT, "nothing to change")
    updated = api.update_atom(
        conn,
        viewer,
        node["id"],
        text=text,
        title=title,
        tags=tags,
        private=private,
        payload_extra=extra,
    )
    if updated["head_record_id"] == node["head_record_id"]:
        return f"unchanged {node['address']}"
    return f"updated {node['kind']} {node['address']} (older versions stay readable by address)"


def link(
    conn: Conn,
    viewer: Account,
    *,
    ref: str,
    to: str,
    rel: str = "part-of",
    remove: bool = False,
) -> str:
    rel = (rel or "part-of").strip()
    node = resolve_ref(conn, viewer, ref)
    if rel == "part-of":
        hub = _resolve_hub(conn, viewer, to, adding=not remove)
        if remove:
            api.remove_membership(conn, viewer, node["id"], hub["id"])
            return f"removed {node['address']} from hub {hub['address']}"
        _require_can_add(conn, viewer, hub)
        own = node["author"] == viewer.person_id
        if not own and node["kind"] == "hub":
            raise ReticulaError(
                FORBIDDEN,
                "you can only file your own hubs under a hub",
                hint="a hub's filing decides who inherits access to what lies"
                " beneath it, so only its author can change it",
            )
        before = conn.execute(
            "SELECT 1 FROM membership WHERE node_id = %s AND hub_node_id = %s"
            " AND author = %s AND NOT withdrawn",
            (node["id"], hub["id"], viewer.person_id),
        ).fetchone()
        api.add_membership(conn, viewer, node["id"], hub["id"])
        verb = "already filed" if before else "filed"
        out = f"{verb} {node['address']} under hub {hub['address']}"
        if own:
            return out
        return (
            f"{out} as your statement, to organise it.\n"
            "Hub members who cannot already read this note will not see it: only"
            " its author can share it into the hub.\n"
            "next: ask the author to link it to the hub themselves"
        )
    target = resolve_ref(conn, viewer, to)
    if node["author"] != viewer.person_id:
        raise ReticulaError(
            FORBIDDEN,
            "only the author can add relations to a note",
            hint="write your own note that supersedes or links to it",
        )
    updated = api.add_relation(
        conn, viewer, node["id"], rel, target["address"], remove=remove
    )
    changed = updated["head_record_id"] != node["head_record_id"]
    verb = ("removed" if remove else "added") if changed else "no change:"
    return f"{verb} {rel} {node['address']} -> {target['address']}"


def delete(conn: Conn, viewer: Account, *, ref: str) -> str:
    node = resolve_ref(conn, viewer, ref)
    if node["author"] != viewer.person_id:
        raise ReticulaError(FORBIDDEN, "only the author can delete a note")
    api.tombstone_atom(conn, viewer, node["id"])
    return (
        f"deleted {node['address']}: the address stays and answers as deleted,"
        " its content is erased"
    )


def _own_private_notes(conn: Conn, viewer: Account, hub_node_id: int) -> list[str]:
    """Addresses of the viewer's OWN private notes filed in the hub or under it
    (newest first). Nobody else's notes are looked at."""
    rows = conn.execute(
        "WITH RECURSIVE sub(id) AS ("
        " SELECT %(hub)s::bigint"
        " UNION SELECT m.node_id FROM membership m JOIN sub ON m.hub_node_id = sub.id"
        "  JOIN node c ON c.id = m.node_id AND c.kind = 'hub'"
        "  WHERE NOT m.withdrawn)"
        " SELECT DISTINCT n.address, n.id FROM node n"
        " JOIN membership m ON m.node_id = n.id AND NOT m.withdrawn"
        " WHERE m.hub_node_id IN (SELECT id FROM sub) AND n.author = %(person)s"
        " AND n.audience = 'private' AND n.tombstoned_at IS NULL"
        " ORDER BY n.id DESC",
        {"hub": hub_node_id, "person": viewer.person_id},
    ).fetchall()
    return [r["address"] for r in rows]


def _handle(conn: Conn, person_id: str) -> str:
    account = get_account_by_person_id(conn, person_id)
    return f"@{account.handle}" if account else person_id


def _list_access(conn: Conn, viewer: Account, hub: Row) -> str:
    """Who has access. The full list goes to the hub's admin and to people who
    can add to it; anyone else sees the owner and their own entry only (a display
    rule: the permissions are unchanged)."""
    mode = access.hub_access(conn, viewer.person_id, hub["id"])
    lines = [f"access to {_label(hub)}", f"{_handle(conn, hub['author'])} admin"]
    if viewer.person_id == hub["author"] or mode == "add":
        rows = conn.execute(
            "SELECT grantee, mode, granted_by FROM hub_grant WHERE hub_node_id = %s"
            " AND mode <> 'none' ORDER BY id",
            (hub["id"],),
        ).fetchall()
        for r in rows:
            lines.append(
                f"{_handle(conn, r['grantee'])} {r['mode']}"
                f" (granted by {_handle(conn, r['granted_by'])})"
            )
        lines.append(
            "add: reads the hub and adds notes of their own. read: reads only."
            " Nobody can edit another person's note."
        )
        lines.append("next: share(hub, person='@handle', mode='add'|'read'|'none')")
    else:
        lines.append(f"@{viewer.handle} {mode or 'read'}")
        lines.append("other readers are shown only to people who can add to this hub")
    return "\n".join(lines)


def share(
    conn: Conn,
    viewer: Account,
    *,
    hub: str,
    person: str | None = None,
    mode: str = "add",
) -> str:
    hub_row = _resolve_hub(conn, viewer, hub, adding=False)
    if person is None:
        return _list_access(conn, viewer, hub_row)
    mode = (mode or "add").strip().lower()
    if mode not in access.MODES:
        raise ReticulaError(BAD_INPUT, "mode must be add, read or none")
    ref = person.strip()
    targets_self = ref in (f"@{viewer.handle}", viewer.person_id)
    # Refuse before looking the person up: a viewer who may not grant must get
    # the same answer whether or not that handle exists or has access.
    access.precheck_grant(
        conn,
        granter=viewer.person_id,
        hub=hub_row,
        mode=mode,
        targets_self=targets_self,
    )
    grantee = resolve_person(conn, person)
    note = ""
    if mode == "add" and (hub_row["payload"] or {}).get("preset") == "personal":
        mode = "read"
        note = (
            "\nnote: only the owner adds in a personal hub, so they got read."
            f' edit(ref={hub_row["address"][-8:]}, preset="group") lets members add.'
        )
    owned_before = (
        len(access.hubs_to_detach(conn, hub_row["id"], grantee.person_id))
        if mode == "none"
        else 0
    )
    api.set_grant(conn, viewer, hub_row["id"], grantee.person_id, mode)
    who = f"@{grantee.handle}"
    where = hub_row["address"]
    if mode == "none":
        left = access.hub_access(conn, grantee.person_id, hub_row["id"])
        extra = (
            f"; {who} still has {left} access through a hub above this one"
            if left
            else ""
        )
        detached = owned_before - len(
            access.hubs_to_detach(conn, hub_row["id"], grantee.person_id)
        )
        if detached > 0:
            extra += (
                f"\n{detached} hub{'s' if detached != 1 else ''} owned by {who}"
                f" {'were' if detached != 1 else 'was'} detached from this hub's"
                " tree; notes others filed there stay readable to their owner"
            )
        return f"{who} no longer has access to {where}{extra}"
    what = "read and add" if mode == "add" else "read"
    out = f"{who} can now {what} in {where} and in the hubs under it{note}"
    hidden = _own_private_notes(conn, viewer, hub_row["id"])
    if hidden:
        out += (
            f"\nnote: {len(hidden)} of your notes in this hub are private and stay"
            " hidden from its members;"
            f" edit(ref={hidden[0][-8:]}, private=false) shows one."
        )
    return out + "\nnext: put(text=..., hub=<this hub>) to add something for them"
