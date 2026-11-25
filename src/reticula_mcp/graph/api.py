"""The graph write API. The tool handlers are thin wrappers over this; other
lanes (content's bootstrap) call it directly.

Every function signs records through :func:`records.sign_and_append` and
materialises them in the same transaction with :func:`materialize.apply_record`.
The caller owns the transaction. The names and signatures of
:func:`create_atom`, :func:`update_atom`, :func:`add_membership` and
:func:`set_grant` are fixed (AGENTS.md, seam 10).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.accounts import Account, get_account_by_person_id
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import (
    BAD_INPUT,
    CONFLICT,
    FORBIDDEN,
    NOT_FOUND,
    ReticulaError,
)
from reticula_mcp.graph import access
from reticula_mcp.graph.materialize import apply_record
from reticula_mcp.graph.paper import normalize_ext_id
from reticula_mcp.permissions import can_read, readable_nodes_sql
from reticula_mcp.resolve import resolve_ref

Row = dict[str, Any]

CORE_KEYS = frozenset({"kind", "text"})
MAX_TEXT = 200_000
MAX_TITLE = 200
MAX_TAGS = 20
MAX_TAG = 50
MAX_ANCHOR = 500
MAX_MODEL = 100
MAX_EXT_ID = 300

#: ``kind`` and a link's ``rel`` are printed in other accounts' tool output, so
#: they are checked on every write (here, not in ``materialize``: a rebuild from
#: records already signed must not start failing).
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


# --- small helpers -------------------------------------------------------------


def node_row(conn: Conn, node_id: int) -> Row:
    row = conn.execute("SELECT * FROM node WHERE id = %s", (node_id,)).fetchone()
    if row is None:
        raise ReticulaError(NOT_FOUND, "no such note")
    return row


def _live_hub(conn: Conn, hub_node_id: int) -> Row:
    hub = conn.execute("SELECT * FROM node WHERE id = %s", (hub_node_id,)).fetchone()
    if hub is None or hub["kind"] != "hub" or hub["tombstoned_at"] is not None:
        raise ReticulaError(NOT_FOUND, "no such hub")
    return hub


def _lock_live_node(conn: Conn, node_id: int) -> Row:
    node = conn.execute(
        "SELECT * FROM node WHERE id = %s FOR UPDATE", (node_id,)
    ).fetchone()
    if node is None or node["tombstoned_at"] is not None:
        raise ReticulaError(NOT_FOUND, "no such note (or it was deleted)")
    return node


def _canonical(payload: Mapping[str, Any]) -> None:
    try:
        fmt.canonical_text(payload)
    except (ValueError, TypeError) as exc:
        raise ReticulaError(BAD_INPUT, f"payload cannot be stored: {exc}") from exc


def _merge_extra(payload: dict[str, Any], extra: Mapping[str, Any] | None) -> None:
    """Merge extra members into a payload; a value of None removes the member."""
    for key, value in (extra or {}).items():
        if key in CORE_KEYS:
            raise ReticulaError(BAD_INPUT, f"payload_extra cannot set {key!r}")
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value


def _check_length(name: str, value: str | None, limit: int) -> None:
    if value is not None and len(value) > limit:
        raise ReticulaError(BAD_INPUT, f"{name} is longer than {limit} characters")


def _check_kind(kind: str) -> None:
    if not KIND_RE.fullmatch(kind):
        raise ReticulaError(
            BAD_INPUT,
            f"kind {kind[:40]!r} is not allowed",
            hint="a kind is 1-32 characters: a lowercase letter, then"
            " lowercase letters, digits, _ or -",
        )


def _check_rel(rel: str) -> None:
    if not REL_RE.fullmatch(rel):
        raise ReticulaError(
            BAD_INPUT,
            f"rel {rel[:40]!r} is not allowed",
            hint="a rel is 1-64 characters from A-Z a-z 0-9 _ . : -",
        )


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for tag in tags or ():
        if not isinstance(tag, str):
            raise ReticulaError(BAD_INPUT, "tags must be strings")
        tag = tag.strip()
        _check_length("a tag", tag, MAX_TAG)
        if tag and tag not in out:
            out.append(tag)
            if len(out) > MAX_TAGS:
                raise ReticulaError(BAD_INPUT, f"more than {MAX_TAGS} tags")
    return out


def _hub_id(hub: Row) -> str:
    return fmt.hub_id_from_address(hub["address"])


def _append(
    conn: Conn,
    account: Account,
    *,
    type: str,
    address: str,
    payload: Mapping[str, Any],
    parents: Sequence[str] = (),
    hub_id: str | None = None,
    audience: str = "hub",
) -> records.Record:
    _canonical(payload)
    return records.sign_and_append(
        conn,
        account,
        type=type,
        address=address,
        payload=payload,
        parents=parents,
        hub_id=hub_id,
        audience=audience,
    )


def _head(conn: Conn, node: Row) -> tuple[records.Record, dict[str, Any]]:
    head = records.head(conn, node["address"])
    if head is None or head.version_hash != node["head_version"] or head.blanked:
        raise ReticulaError(
            "Internal",
            f"{node['address']} is out of step with the record log",
            hint="run `reticula-mcp rebuild`",
        )
    payload = head.payload
    if not isinstance(payload, dict):
        raise ReticulaError("Internal", "stored payload is not an object")
    return head, dict(payload)


def _new_version(
    conn: Conn,
    author: Account,
    node: Row,
    head: records.Record,
    payload: Mapping[str, Any],
    audience: str,
) -> Row:
    rec = _append(
        conn,
        author,
        type="atom",
        address=node["address"],
        payload=payload,
        parents=[head.version_hash],
        hub_id=head.hub_id,
        audience=audience,
    )
    apply_record(conn, rec)
    return node_row(conn, node["id"])


# --- atoms ---------------------------------------------------------------------


def find_readable_paper(conn: Conn, viewer: Account, ext_key: str) -> Row | None:
    """The oldest paper stub with this ``ext_key`` that ``viewer`` can read."""
    return conn.execute(
        "SELECT n.* FROM node n WHERE n.kind = 'paper' AND n.ext_key = %s"
        f" AND n.id IN ({readable_nodes_sql('%s')}) ORDER BY n.id LIMIT 1",
        (ext_key, viewer.id),
    ).fetchone()


def create_atom(
    conn: Conn,
    author: Account,
    *,
    kind: str,
    text: str,
    title: str | None = None,
    tags: Sequence[str] | None = None,
    hub_node_id: int | None = None,
    private: bool = False,
    supersedes: Sequence[str] = (),
    ext_id: str | None = None,
    anchor: str | None = None,
    model: str | None = None,
    payload_extra: Mapping[str, Any] | None = None,
) -> Row:
    """Write a new atom and, when ``hub_node_id`` is given, file it there.

    ``supersedes`` lists addresses the author can read. A ``paper`` whose
    ``ext_id`` matches a paper the author can already read returns that paper
    and writes nothing. Returns the ``node`` row.
    """
    kind = (kind or "").strip()
    if not kind:
        raise ReticulaError(BAD_INPUT, "kind is empty")
    _check_kind(kind)
    text = text or ""
    _check_length("text", text, MAX_TEXT)
    title = title.strip() if title else None
    _check_length("title", title, MAX_TITLE)
    _check_length("anchor", anchor, MAX_ANCHOR)
    _check_length("model", model, MAX_MODEL)
    _check_length("ext_id", ext_id, MAX_EXT_ID)
    tag_list = _clean_tags(tags)
    hub: Row | None = None
    if hub_node_id is not None:
        hub = _live_hub(conn, hub_node_id)
        if not access.can_add_person(conn, author.person_id, hub["id"]):
            raise ReticulaError(FORBIDDEN, "you cannot add to that hub")
    ext_key: str | None = None
    ext: dict[str, str] | None = None
    if ext_id is not None:
        ext_key, ext = normalize_ext_id(ext_id)
    if kind == "hub" and not title:
        raise ReticulaError(BAD_INPUT, "a hub needs a title")
    if kind == "paper":
        if not title or ext_key is None:
            raise ReticulaError(BAD_INPUT, "a paper needs a title and an ext_id")
        existing = find_readable_paper(conn, author, ext_key)
        if existing is not None:
            if hub is not None and existing["author"] == author.person_id:
                add_membership(conn, author, existing["id"], hub["id"])
            return existing
    elif not text.strip() and kind != "hub":
        raise ReticulaError(BAD_INPUT, "text is empty")
    supersedes_addresses: list[str] = []
    for ref in supersedes:
        old = resolve_ref(conn, author, ref)
        if old["address"] not in supersedes_addresses:
            supersedes_addresses.append(old["address"])

    payload: dict[str, Any] = {}
    _merge_extra(payload, payload_extra)
    payload.update(kind=kind, text=text)
    if title:
        payload["title"] = title
    if tag_list:
        payload["tags"] = tag_list
    if supersedes_addresses:
        payload["supersedes"] = supersedes_addresses
    if ext is not None:
        payload["ext"] = ext
    if anchor:
        payload["anchor"] = anchor.strip()
    payload["origin"] = {"by": "agent", "model": model} if model else {"by": "person"}
    address = fmt.new_address(author.person_id)
    rec = _append(
        conn,
        author,
        type="atom",
        address=address,
        payload=payload,
        hub_id=_hub_id(hub) if hub else None,
        audience="private" if private else "hub",
    )
    node_id = apply_record(conn, rec)
    assert node_id is not None
    if hub is not None:
        add_membership(conn, author, node_id, hub["id"])
    return node_row(conn, node_id)


def update_atom(
    conn: Conn,
    author: Account,
    node_id: int,
    *,
    text: str | None = None,
    title: str | None = None,
    tags: Sequence[str] | None = None,
    private: bool | None = None,
    payload_extra: Mapping[str, Any] | None = None,
) -> Row:
    """Append a new version of one of your atoms (``parents`` is its head).

    Takes a row lock on the node first, so two concurrent edits form a chain.
    Returns the ``node`` row; when nothing changes no version is written.
    """
    node = _lock_live_node(conn, node_id)
    if node["author"] != author.person_id:
        raise ReticulaError(FORBIDDEN, "only the author can edit a note")
    head, payload = _head(conn, node)
    new = dict(payload)
    if text is not None:
        _check_length("text", text, MAX_TEXT)
        new["text"] = text
    if title is not None:
        _check_length("title", title.strip(), MAX_TITLE)
        if title.strip():
            new["title"] = title.strip()
        elif node["kind"] in ("hub", "paper"):
            raise ReticulaError(BAD_INPUT, f"a {node['kind']} needs a title")
        else:
            new.pop("title", None)
    if tags is not None:
        tag_list = _clean_tags(tags)
        if tag_list:
            new["tags"] = tag_list
        else:
            new.pop("tags", None)
    _merge_extra(new, payload_extra)
    audience = head.audience
    if private is not None:
        audience = "private" if private else "hub"
    if new == payload and audience == head.audience:
        return node
    if node["kind"] != "hub" and not str(new.get("text", "")).strip():
        raise ReticulaError(BAD_INPUT, "text is empty")
    return _new_version(conn, author, node, head, new, audience)


def add_relation(
    conn: Conn,
    author: Account,
    node_id: int,
    rel: str,
    to_address: str,
    *,
    remove: bool = False,
) -> Row:
    """Add (or remove) a relation in your own atom's payload as a new version.

    ``supersedes`` goes to the payload's ``supersedes`` list (and the
    ``supersedes`` table); any other ``rel`` to ``links``.
    """
    rel = rel.strip()
    if not remove:  # taking back a link written before the check stays possible
        _check_rel(rel)
    if not fmt.is_valid_address(to_address):
        raise ReticulaError(BAD_INPUT, f"not an address: {to_address!r}")
    node = _lock_live_node(conn, node_id)
    if node["author"] != author.person_id:
        raise ReticulaError(FORBIDDEN, "only the author can change a note's links")
    if to_address == node["address"]:
        raise ReticulaError(BAD_INPUT, "a note cannot point at itself")
    head, payload = _head(conn, node)
    new = dict(payload)
    items: list[Any]
    if rel == "supersedes":
        key = "supersedes"
        items = [a for a in new.get("supersedes", []) if isinstance(a, str)]
        present = to_address in items
        if remove:
            items = [a for a in items if a != to_address]
        elif not present:
            items.append(to_address)
    else:
        key = "links"
        items = [x for x in new.get("links", []) if isinstance(x, dict)]
        present = any(x.get("rel") == rel and x.get("to") == to_address for x in items)
        if remove:
            items = [
                x
                for x in items
                if not (x.get("rel") == rel and x.get("to") == to_address)
            ]
        elif not present:
            items.append({"rel": rel, "to": to_address})
    if (remove and not present) or (not remove and present):
        return node
    if items:
        new[key] = items
    else:
        new.pop(key, None)
    return _new_version(conn, author, node, head, new, head.audience)


def tombstone_atom(conn: Conn, author: Account, node_id: int) -> Row:
    """Delete one of your own atoms: a terminal version of its address.

    The node keeps its address and loses its content; the stored envelopes of
    every earlier version are erased (``blank_record_content``).
    """
    node = _lock_live_node(conn, node_id)
    if node["author"] != author.person_id:
        raise ReticulaError(FORBIDDEN, "only the author can delete a note")
    head, _ = _head(conn, node)
    previous = [r.id for r in records.versions(conn, node["address"])]
    rec = _append(
        conn,
        author,
        type="tombstone",
        address=node["address"],
        payload={"derived": False, "kind": node["kind"]},
        parents=[head.version_hash],
        hub_id=head.hub_id,
        audience=head.audience,
    )
    apply_record(conn, rec)
    for record_id in previous:
        conn.execute("SELECT blank_record_content(%s)", (record_id,))
    return node_row(conn, node["id"])


# --- memberships ---------------------------------------------------------------


def _member_payload(atom: Row, hub: Row, withdrawn: bool) -> dict[str, Any]:
    return {"atom": atom["address"], "hub": hub["address"], "withdrawn": withdrawn}


def add_membership(conn: Conn, actor: Account, node_id: int, hub_node_id: int) -> None:
    """File an atom under a hub you can add to, as a signed statement of yours.

    The atom's author may file their own atoms and hubs. Anyone else may file
    an atom (never a hub) they can read: the statement organises but confers no
    access, because ``readable_nodes`` honours only statements by the atom's own
    author. Idempotent per actor. Refuses a hub filing that would close a cycle
    in the hub hierarchy (the hierarchy is locked while that is checked).
    """
    kind = conn.execute("SELECT kind FROM node WHERE id = %s", (node_id,)).fetchone()
    if kind is None:
        raise ReticulaError(NOT_FOUND, "no such note")
    if kind["kind"] == "hub":
        access.lock_hierarchy(conn)
    node = _lock_live_node(conn, node_id)
    hub = _live_hub(conn, hub_node_id)
    if node["author"] != actor.person_id:
        if node["kind"] == "hub":
            raise ReticulaError(
                FORBIDDEN,
                "you can only file your own hubs under a hub",
                hint="a hub's filing decides who inherits access to what lies"
                " beneath it, so only its author can change it",
            )
        if not can_read(conn, actor, node["id"]):
            raise ReticulaError(NOT_FOUND, "no such note")
    if not access.can_add_person(conn, actor.person_id, hub["id"]):
        raise ReticulaError(FORBIDDEN, "you cannot add to that hub")
    if not access.is_hub_admin(
        conn, actor.person_id, hub["id"]
    ) and access.removed_by_admin(conn, node["id"], hub["id"]):
        raise ReticulaError(
            FORBIDDEN,
            "an admin of that hub removed this note from it; only an admin can"
            " file it there again",
        )
    if node["kind"] == "hub" and access.closes_cycle(conn, node["id"], hub["id"]):
        raise ReticulaError(
            CONFLICT,
            "that would put a hub inside itself",
            hint="a hub cannot be filed under itself or under one of its own sub-hubs",
        )
    own = conn.execute(
        "SELECT address, withdrawn FROM membership WHERE node_id = %s"
        " AND hub_node_id = %s AND author = %s ORDER BY id LIMIT 1",
        (node["id"], hub["id"], actor.person_id),
    ).fetchone()
    if own is not None and not own["withdrawn"]:
        return
    audience = "hub"
    if own is not None:
        head = records.head(conn, own["address"])
        assert head is not None
        parents: Sequence[str] = [head.version_hash]
        address = own["address"]
    else:
        parents = ()
        address = fmt.new_address(actor.person_id)
    rec = _append(
        conn,
        actor,
        type="member",
        address=address,
        payload=_member_payload(node, hub, False),
        parents=parents,
        hub_id=_hub_id(hub),
        audience=audience,
    )
    apply_record(conn, rec)


def remove_membership(
    conn: Conn, actor: Account, node_id: int, hub_node_id: int
) -> None:
    """Withdraw an atom from a hub.

    Allowed to the author of a statement (for their own), to the hub's admin and
    to the atom's author (for every statement about the atom in that hub).
    """
    node = node_row(conn, node_id)
    hub = conn.execute(
        "SELECT * FROM node WHERE id = %s AND kind = 'hub'", (hub_node_id,)
    ).fetchone()
    if hub is None:
        raise ReticulaError(NOT_FOUND, "no such hub")
    rows = conn.execute(
        "SELECT address, author FROM membership WHERE node_id = %s"
        " AND hub_node_id = %s AND NOT withdrawn ORDER BY id",
        (node_id, hub_node_id),
    ).fetchall()
    if not rows:
        raise ReticulaError(NOT_FOUND, "that note is not filed under that hub")
    mine = [r for r in rows if r["author"] == actor.person_id]
    privileged = actor.person_id == node["author"] or access.is_hub_admin(
        conn, actor.person_id, hub["id"]
    )
    if not mine and not privileged:
        raise ReticulaError(
            FORBIDDEN,
            "only the person who filed it, the hub's admin or the note's author"
            " can remove it",
        )
    for row in mine:
        head = records.head(conn, row["address"])
        assert head is not None
        rec = _append(
            conn,
            actor,
            type="member",
            address=row["address"],
            payload=_member_payload(node, hub, True),
            parents=[head.version_hash],
            hub_id=_hub_id(hub),
            audience="hub",
        )
        apply_record(conn, rec)
    if not privileged:
        return
    # the hub's admin and the note's author also clear everyone else's statements
    left = conn.execute(
        "SELECT 1 FROM membership WHERE node_id = %s AND hub_node_id = %s"
        " AND NOT withdrawn LIMIT 1",
        (node_id, hub_node_id),
    ).fetchone()
    if left is None:
        return
    rec = _append(
        conn,
        actor,
        type="member",
        address=fmt.new_address(actor.person_id),
        payload=_member_payload(node, hub, True),
        hub_id=_hub_id(hub),
        audience="hub",
    )
    apply_record(conn, rec)


# --- grants --------------------------------------------------------------------


def set_grant(
    conn: Conn,
    actor: Account,
    hub_node_id: int,
    grantee_person_id: str,
    mode: str,
) -> None:
    """Set a person's access to a hub: ``add``, ``read`` or ``none``.

    See :func:`access.authorize_grant` for who may do what. A grant covers the
    hub and everything filed beneath it. No record is written when the
    person already has exactly this access.
    """
    hub = conn.execute(
        "SELECT * FROM node WHERE id = %s FOR UPDATE", (hub_node_id,)
    ).fetchone()
    if hub is None or hub["kind"] != "hub":
        raise ReticulaError(NOT_FOUND, "no such hub")
    if not fmt.is_person_id(grantee_person_id):
        raise ReticulaError(BAD_INPUT, "grantee must be a person ID")
    if get_account_by_person_id(conn, grantee_person_id) is None:
        raise ReticulaError(NOT_FOUND, "no such person on this instance")
    access.authorize_grant(
        conn, granter=actor.person_id, hub=hub, grantee=grantee_person_id, mode=mode
    )
    existing = conn.execute(
        "SELECT mode FROM hub_grant WHERE hub_node_id = %s AND grantee = %s",
        (hub["id"], grantee_person_id),
    ).fetchone()
    if (existing and existing["mode"] == mode) or (existing is None and mode == "none"):
        return
    rec = _append(
        conn,
        actor,
        type="grant",
        address=fmt.new_address(actor.person_id),
        payload={
            "hub": hub["address"],
            "to": grantee_person_id,
            "mode": mode,
            "wrapped_key": None,
        },
        hub_id=_hub_id(hub),
        audience="hub",
    )
    apply_record(conn, rec)
    if (
        mode == "none"
        and actor.person_id == hub["author"]
        and grantee_person_id != actor.person_id
    ):
        _detach_owned_hubs(conn, actor, hub, grantee_person_id)


def _detach_owned_hubs(conn: Conn, admin: Account, hub: Row, person_id: str) -> None:
    """After the admin revoked ``person_id`` on ``hub``: take every hub the person
    owns out from under ``hub`` and the hubs below it, so their revoked access does
    not live on through a sub-hub they can still invite into. Notes other people
    filed into those hubs stay readable to the hub's owner."""
    for row in access.hubs_to_detach(conn, hub["id"], person_id):
        child = node_row(conn, row["node_id"])
        parent = node_row(conn, row["hub_node_id"])
        rec = _append(
            conn,
            admin,
            type="member",
            address=fmt.new_address(admin.person_id),
            payload=_member_payload(child, parent, True),
            hub_id=_hub_id(parent),
            audience="hub",
        )
        apply_record(conn, rec)
