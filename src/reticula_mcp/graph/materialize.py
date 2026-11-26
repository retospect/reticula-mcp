"""Materialise signed records into the graph tables, and rebuild them.

``node``, ``membership``, ``hub_grant``, ``supersedes`` and ``link`` are views
of the ``record`` log: every table row is a pure function of the records
applied so far, in log order. :func:`apply_record` applies ONE record (every
write path calls it in the writing transaction); :func:`rebuild` resets the
tables and replays the whole log through the same function.

Authority is checked here as well as in the handlers, as of the moment the
record is applied (the log order), so a replay reproduces exactly what the
original writes produced and a record ingested from elsewhere cannot widen
anyone's access:

* a membership statement is stored when its author could add to the hub at that
  moment (and, for a hub, is the hub's author); it confers access only when
  filed by the atom's own author (see ``readable_nodes``);
* a withdrawal is honoured from the statement's author, the admin of the hub
  (or of a hub above it) and the atom's author; an atom an admin removed can be
  filed there again only by an admin;
* a grant is applied only when :func:`access.authorize_grant` allows it.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import access
from reticula_mcp.graph.paper import ext_key_from_payload
from reticula_mcp.search import hooks

log = logging.getLogger(__name__)
Row = dict[str, Any]

# What the rebuild reads: every type that changes graph state.
STATE_TYPES = ("atom", "member", "grant", "tombstone")
#: The grantee of a grant that puts a hub on the public page (0354). Never a
#: person ID, so no account's readable_hubs ever matches it.
PUBLIC = "public"


def node_by_address(conn: Conn, address: str) -> Row | None:
    return conn.execute("SELECT * FROM node WHERE address = %s", (address,)).fetchone()


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def apply_record(conn: Conn, rec: records.Record, *, notify: bool = True) -> int | None:
    """Apply one record to the graph tables. Returns the id of the node whose
    row was created or changed (atoms, tombstones), else None.

    Unknown types, blanked records and records that fail the authority checks
    change nothing.
    """
    if rec.type not in STATE_TYPES or rec.blanked:
        return None
    try:
        payload = rec.payload
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    node_id: int | None = None
    if rec.type == "atom":
        node_id = _apply_atom(conn, rec, payload)
    elif rec.type == "tombstone":
        node_id = _apply_tombstone(conn, rec, payload)
    elif rec.type == "member":
        _apply_member(conn, rec, payload)
    elif rec.type == "grant":
        _apply_grant(conn, rec, payload)
    if node_id is not None and notify:
        hooks.on_node_changed(conn, node_id)
    return node_id


# --- atoms ------------------------------------------------------------------


def _apply_atom(conn: Conn, rec: records.Record, payload: Row) -> int:
    kind = _text(payload.get("kind")) or "note"
    ext_key = ext_key_from_payload(payload) if kind == "paper" else None
    title = payload.get("title")
    row = conn.execute(
        "INSERT INTO node (address, author, author_account_id, kind, head_record_id,"
        " head_version, title, text, tags, payload, ext_key, audience, tombstoned_at,"
        " created_at, updated_at)"
        " VALUES (%(address)s, %(author)s,"
        " (SELECT id FROM account WHERE person_id = %(author)s), %(kind)s,"
        " %(record_id)s, %(version)s, %(title)s, %(text)s, %(tags)s, %(payload)s,"
        " %(ext_key)s, %(audience)s, NULL, %(at)s, %(at)s)"
        " ON CONFLICT (address) DO UPDATE SET"
        " author_account_id = EXCLUDED.author_account_id, kind = EXCLUDED.kind,"
        " head_record_id = EXCLUDED.head_record_id,"
        " head_version = EXCLUDED.head_version, title = EXCLUDED.title,"
        " text = EXCLUDED.text, tags = EXCLUDED.tags, payload = EXCLUDED.payload,"
        " ext_key = EXCLUDED.ext_key, audience = EXCLUDED.audience,"
        " updated_at = EXCLUDED.updated_at"
        " RETURNING id",
        {
            "address": rec.address,
            "author": rec.author,
            "kind": kind,
            "record_id": rec.id,
            "version": rec.version_hash,
            "title": title if isinstance(title, str) else None,
            "text": _text(payload.get("text")),
            "tags": _strings(payload.get("tags")),
            "payload": Jsonb(payload),
            "ext_key": ext_key,
            "audience": rec.audience,
            "at": rec.created_at,
        },
    ).fetchone()
    assert row is not None
    node_id = int(row["id"])
    _refresh_relations(conn, node_id, payload)
    return node_id


def _refresh_relations(conn: Conn, node_id: int, payload: Row) -> None:
    """``supersedes`` and ``link`` rows are a function of the head payload."""
    conn.execute("DELETE FROM supersedes WHERE new_node_id = %s", (node_id,))
    for address in dict.fromkeys(_strings(payload.get("supersedes"))):
        old = node_by_address(conn, address)
        if old is not None and old["id"] != node_id:
            conn.execute(
                "INSERT INTO supersedes (new_node_id, old_node_id) VALUES (%s, %s)"
                " ON CONFLICT DO NOTHING",
                (node_id, old["id"]),
            )
    conn.execute("DELETE FROM link WHERE src_node_id = %s", (node_id,))
    links = payload.get("links")
    if not isinstance(links, list):
        return
    for item in links:
        if not isinstance(item, dict):
            continue
        rel, to = item.get("rel"), item.get("to")
        if not isinstance(rel, str) or not isinstance(to, str) or not rel or not to:
            continue
        dst = node_by_address(conn, to)
        conn.execute(
            "INSERT INTO link (src_node_id, rel, dst_address, dst_node_id)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (node_id, rel, to, dst["id"] if dst else None),
        )


def _apply_tombstone(conn: Conn, rec: records.Record, payload: Row) -> int:
    """Keep the address; drop the content. A tombstone is terminal."""
    first = conn.execute(
        "SELECT min(created_at) AS at FROM record WHERE address = %s", (rec.address,)
    ).fetchone()
    created = first["at"] if first and first["at"] else rec.created_at
    row = conn.execute(
        "INSERT INTO node (address, author, author_account_id, kind, head_record_id,"
        " head_version, title, text, tags, payload, ext_key, audience, tombstoned_at,"
        " created_at, updated_at)"
        " VALUES (%(address)s, %(author)s,"
        " (SELECT id FROM account WHERE person_id = %(author)s), %(kind)s,"
        " %(record_id)s, %(version)s, NULL, '', '{}', '{}'::jsonb, NULL,"
        " %(audience)s, %(at)s, %(created)s, %(at)s)"
        " ON CONFLICT (address) DO UPDATE SET"
        " kind = CASE WHEN node.kind = 'unreplayed' THEN EXCLUDED.kind"
        "   ELSE node.kind END,"
        " audience = CASE WHEN node.kind = 'unreplayed' THEN EXCLUDED.audience"
        "   ELSE node.audience END,"
        " head_record_id = EXCLUDED.head_record_id,"
        " head_version = EXCLUDED.head_version, title = NULL, text = '',"
        " tags = '{}', payload = '{}'::jsonb, ext_key = NULL,"
        " tombstoned_at = EXCLUDED.tombstoned_at, updated_at = EXCLUDED.updated_at"
        " RETURNING id",
        {
            "address": rec.address,
            "author": rec.author,
            "kind": _text(payload.get("kind")) or "note",
            "record_id": rec.id,
            "version": rec.version_hash,
            "audience": rec.audience,
            "at": rec.created_at,
            "created": created,
        },
    ).fetchone()
    assert row is not None
    node_id = int(row["id"])
    conn.execute("DELETE FROM supersedes WHERE new_node_id = %s", (node_id,))
    conn.execute("DELETE FROM link WHERE src_node_id = %s", (node_id,))
    return node_id


# --- memberships --------------------------------------------------------------


def _apply_member(conn: Conn, rec: records.Record, payload: Row) -> None:
    atom = node_by_address(conn, _text(payload.get("atom")))
    hub = node_by_address(conn, _text(payload.get("hub")))
    if atom is None or hub is None or hub["kind"] != "hub":
        return
    withdrawn = payload.get("withdrawn") is True
    existing = conn.execute(
        "SELECT node_id, hub_node_id FROM membership WHERE address = %s",
        (rec.address,),
    ).fetchone()
    if existing is not None and (
        existing["node_id"] != atom["id"] or existing["hub_node_id"] != hub["id"]
    ):
        # A statement is about one atom in one hub, for good: a later version of
        # its address that names another atom or hub is refused, not half-applied.
        return
    # The hub's admin (the author of the hub or of a hub above it) and the atom's
    # author may withdraw every statement about the atom in that hub; anyone else
    # only their own.
    is_admin = access.is_hub_admin(conn, rec.author, hub["id"])
    privileged = is_admin or rec.author == atom["author"]
    moderated = False
    if withdrawn:
        if existing is None and not privileged:
            return  # a stranger's withdrawal of someone else's filing: ignored
        if privileged and existing is None:
            # a withdrawal under a new address is a moderation act: it clears
            # every statement about the atom in that hub (a version of one's
            # own statement withdraws only that statement)
            conn.execute(
                "UPDATE membership SET withdrawn = true WHERE node_id = %s"
                " AND hub_node_id = %s AND NOT withdrawn",
                (atom["id"], hub["id"]),
            )
            # an admin removing someone else's atom: only an admin refiles it
            moderated = is_admin and rec.author != atom["author"]
    else:
        # Anyone who can add to the hub may file an atom there (organising only:
        # readable_nodes honours the atom author's own statements alone), but a
        # hub's filing drives grant inheritance, so only its author files it.
        if rec.author != atom["author"] and atom["kind"] == "hub":
            return
        if not access.can_add_person(conn, rec.author, hub["id"]):
            return
        if is_admin:
            conn.execute(
                "UPDATE membership SET moderated = false WHERE node_id = %s"
                " AND hub_node_id = %s AND moderated",
                (atom["id"], hub["id"]),
            )
        elif access.removed_by_admin(conn, atom["id"], hub["id"]):
            return
    conn.execute(
        "INSERT INTO membership (address, node_id, hub_node_id, author, audience,"
        " withdrawn, moderated, record_id, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (address) DO UPDATE SET audience = EXCLUDED.audience,"
        " withdrawn = EXCLUDED.withdrawn, record_id = EXCLUDED.record_id",
        (
            rec.address,
            atom["id"],
            hub["id"],
            rec.author,
            rec.audience,
            withdrawn,
            moderated,
            rec.id,
            rec.created_at,
        ),
    )


# --- grants -------------------------------------------------------------------


def _apply_grant(conn: Conn, rec: records.Record, payload: Row) -> None:
    hub = node_by_address(conn, _text(payload.get("hub")))
    grantee = _text(payload.get("to"))
    mode = _text(payload.get("mode"))
    if hub is None:
        return
    if payload.get("answers") is not None:
        _answer_offer(conn, rec, payload, hub)
        return
    if grantee == PUBLIC:
        # the public page: kept only when the hub's author gave read or none
        if rec.author != hub["author"] or mode not in ("read", "none"):
            log.info("grant %s not applied: public needs the owner", rec.address)
            return
    elif not fmt.is_person_id(grantee):
        return
    else:
        if mode == "none" and _withdraw_own(conn, rec, hub, grantee):
            return
        try:
            access.authorize_grant(
                conn, granter=rec.author, hub=hub, grantee=grantee, mode=mode
            )
        except ReticulaError as exc:
            log.info("grant %s not applied: %s", rec.address, exc.message)
            return
        if mode in ("read", "add") and access.share_waits(
            conn,
            rec.id,
            hub["id"],
            rec.author,
            grantee,
            payload.get("pending") is True,
        ):
            _offer(conn, rec, hub, grantee, mode)
            return
        # a grant replaces a share that waits; none takes it back, but leaves a
        # refused one: a share again must not bring it back
        conn.execute(
            "DELETE FROM share_offer WHERE hub_node_id = %s AND grantee = %s"
            + (" AND NOT refused" if mode == "none" else ""),
            (hub["id"], grantee),
        )
        if (
            mode == "none"
            and not conn.execute(
                "SELECT 1 FROM hub_grant WHERE hub_node_id = %s AND grantee = %s",
                (hub["id"], grantee),
            ).fetchone()
        ):
            return  # it only took back a share that waited: no revoked row
    conn.execute(
        "INSERT INTO hub_grant (hub_node_id, grantee, grantee_account_id, mode,"
        " granted_by, address, record_id, created_at, updated_at)"
        " VALUES (%(hub)s, %(grantee)s,"
        " (SELECT id FROM account WHERE person_id = %(grantee)s), %(mode)s,"
        " %(by)s, %(address)s, %(record_id)s, %(at)s, %(at)s)"
        " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = EXCLUDED.mode,"
        " granted_by = EXCLUDED.granted_by, address = EXCLUDED.address,"
        " record_id = EXCLUDED.record_id, updated_at = EXCLUDED.updated_at,"
        " grantee_account_id = EXCLUDED.grantee_account_id",
        {
            "hub": hub["id"],
            "grantee": grantee,
            "mode": mode,
            "by": rec.author,
            "address": rec.address,
            "record_id": rec.id,
            "at": rec.created_at,
        },
    )
    if mode in ("read", "add") and fmt.is_person_id(grantee):
        _lift(conn, rec.author, grantee)


def _withdraw_own(conn: Conn, rec: records.Record, hub: Row, grantee: str) -> bool:
    """A sharer who is not the hub's admin takes back their own share that
    waits (``none``): removed, and nothing else happens. A refused share
    stays."""
    if rec.author in (hub["author"], grantee):
        return False
    gone = conn.execute(
        "DELETE FROM share_offer WHERE hub_node_id = %s AND grantee = %s"
        " AND granted_by = %s AND NOT refused RETURNING 1",
        (hub["id"], grantee, rec.author),
    ).fetchone()
    return gone is not None


def _lift(conn: Conn, granter: str, grantee: str) -> None:
    """A grant from ``granter`` to ``grantee`` that applied (at once, or
    accepted) lifts the block ``granter`` put on ``grantee``: their held
    shares show again."""
    lifted = conn.execute(
        "DELETE FROM share_block WHERE person = %s AND blocked = %s RETURNING 1",
        (granter, grantee),
    ).fetchone()
    if lifted:
        conn.execute(
            "UPDATE share_offer SET held = false WHERE grantee = %s AND granted_by = %s",
            (granter, grantee),
        )


def _offer(conn: Conn, rec: records.Record, hub: Row, grantee: str, mode: str) -> None:
    """A share that waits for its grantee (migration 0304): a share_offer row,
    never a hub_grant row, so nothing of the hub reaches the grantee until a
    record of theirs accepts it. Nothing at all when the grantee blocked the
    sharer. A share from someone the grantee blocked is kept ``held``: the
    sharer sees it wait like any other, the grantee never sees it.

    A share again keeps its place (created_at) and never brings back one the
    grantee refused. Only the hub's admin replaces a share of someone else's
    that waits."""
    existing = conn.execute(
        "SELECT granted_by, refused FROM share_offer WHERE hub_node_id = %s"
        " AND grantee = %s FOR UPDATE",
        (hub["id"], grantee),
    ).fetchone()
    if existing is not None and existing["refused"]:
        log.info("grant %s not applied: refused before", rec.address)
        return
    if (
        existing is not None
        and existing["granted_by"] != rec.author
        and rec.author != hub["author"]
    ):
        log.info("grant %s not applied: someone else's share waits", rec.address)
        return
    conn.execute(
        "INSERT INTO share_offer (hub_node_id, grantee, grantee_account_id, mode,"
        " granted_by, address, record_id, created_at, held)"
        " VALUES (%(hub)s, %(grantee)s,"
        " (SELECT id FROM account WHERE person_id = %(grantee)s), %(mode)s,"
        " %(by)s, %(address)s, %(record_id)s, %(at)s,"
        " EXISTS (SELECT 1 FROM share_block WHERE person = %(grantee)s"
        "  AND blocked = %(by)s))"
        " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = EXCLUDED.mode,"
        " granted_by = EXCLUDED.granted_by, address = EXCLUDED.address,"
        " record_id = EXCLUDED.record_id, held = EXCLUDED.held",
        {
            "hub": hub["id"],
            "grantee": grantee,
            "mode": mode,
            "by": rec.author,
            "address": rec.address,
            "record_id": rec.id,
            "at": rec.created_at,
        },
    )


def _answer_offer(conn: Conn, rec: records.Record, payload: Row, hub: Row) -> None:
    """The grantee's own answer to the share that waits for them: ``answers``
    names the address of that share's record, so an answer never applies to a
    later share. ``read``/``add`` accept it at the lower of the offered and
    the asked mode, checked again against what the sharer may grant now;
    ``none`` refuses it (the row stays, ``refused``), and with ``blocks``
    also stops later shares from the sharer. A share already refused takes
    only a block; a held one takes nothing."""
    offer = conn.execute(
        "SELECT * FROM share_offer WHERE hub_node_id = %s AND grantee = %s"
        " AND address = %s",
        (hub["id"], rec.author, _text(payload.get("answers"))),
    ).fetchone()
    if offer is None or _text(payload.get("to")) != rec.author:
        log.info("answer %s not applied: no such share waits", rec.address)
        return
    asked = _text(payload.get("mode"))
    blocks = payload.get("blocks") is True and asked not in ("read", "add")
    if offer["refused"] or offer["held"]:
        if offer["refused"] and blocks:
            _block(conn, rec, offer["granted_by"])
        else:
            log.info("answer %s not applied: the share was answered", rec.address)
        return
    if asked not in ("read", "add"):
        conn.execute(
            "UPDATE share_offer SET refused = true WHERE hub_node_id = %s"
            " AND grantee = %s",
            (hub["id"], rec.author),
        )
        if blocks:
            _block(conn, rec, offer["granted_by"])
        return
    conn.execute(
        "DELETE FROM share_offer WHERE hub_node_id = %s AND grantee = %s",
        (hub["id"], rec.author),
    )
    mode = "add" if asked == "add" and offer["mode"] == "add" else "read"
    try:
        access.authorize_grant(
            conn, granter=offer["granted_by"], hub=hub, grantee=rec.author, mode=mode
        )
    except ReticulaError as exc:
        log.info("answer %s not applied: %s", rec.address, exc.message)
        return
    conn.execute(
        "INSERT INTO hub_grant (hub_node_id, grantee, grantee_account_id, mode,"
        " granted_by, address, record_id, created_at, updated_at)"
        " VALUES (%(hub)s, %(grantee)s,"
        " (SELECT id FROM account WHERE person_id = %(grantee)s), %(mode)s,"
        " %(by)s, %(address)s, %(record_id)s, %(at)s, %(at)s)"
        " ON CONFLICT (hub_node_id, grantee) DO UPDATE SET mode = EXCLUDED.mode,"
        " granted_by = EXCLUDED.granted_by, address = EXCLUDED.address,"
        " record_id = EXCLUDED.record_id, updated_at = EXCLUDED.updated_at,"
        " grantee_account_id = EXCLUDED.grantee_account_id",
        {
            "hub": hub["id"],
            "grantee": rec.author,
            "mode": mode,
            "by": offer["granted_by"],
            "address": rec.address,
            "record_id": rec.id,
            "at": rec.created_at,
        },
    )
    _lift(conn, offer["granted_by"], rec.author)


def _block(conn: Conn, rec: records.Record, sharer: str) -> None:
    """The author of ``rec`` blocks ``sharer``: every share of theirs to the
    author is held, those that wait now and those to come."""
    conn.execute(
        "INSERT INTO share_block (person, blocked, record_id, created_at)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (person, blocked) DO NOTHING",
        (rec.author, sharer, rec.id, rec.created_at),
    )
    conn.execute(
        "UPDATE share_offer SET held = true WHERE grantee = %s AND granted_by = %s",
        (rec.author, sharer),
    )


# --- rebuild ------------------------------------------------------------------


def _seed_tombstoned(conn: Conn) -> None:
    """Give every tombstoned address a placeholder node before the replay.

    The content of its earlier versions was erased, so nothing in the log would
    create the node until the tombstone itself is replayed; but filings and
    grants that mention it come earlier. The tombstone payload carries the kind.
    """
    for row in conn.execute(
        "SELECT id FROM record WHERE type = 'tombstone'"
    ).fetchall():
        rec = records.get_record(conn, row["id"])
        if rec is None or rec.blanked:
            continue
        try:
            payload = rec.payload
        except ValueError:
            continue
        kind = _text(payload.get("kind")) if isinstance(payload, dict) else ""
        first = conn.execute(
            "SELECT id, version_hash, created_at FROM record WHERE address = %s"
            " ORDER BY id LIMIT 1",
            (rec.address,),
        ).fetchone()
        assert first is not None
        conn.execute(
            "INSERT INTO node (address, author, author_account_id, kind,"
            " head_record_id, head_version, audience, created_at, updated_at)"
            " VALUES (%(address)s, %(author)s,"
            " (SELECT id FROM account WHERE person_id = %(author)s), %(kind)s,"
            " %(id)s, %(version)s, %(audience)s, %(at)s, %(at)s)"
            " ON CONFLICT (address) DO UPDATE SET kind = EXCLUDED.kind,"
            " audience = EXCLUDED.audience",
            {
                "address": rec.address,
                "author": rec.author,
                "kind": kind or "note",
                "id": first["id"],
                "version": first["version_hash"],
                "audience": rec.audience,
                "at": first["created_at"],
            },
        )


def rebuild(conn: Conn) -> dict[str, int]:
    """Reset the graph tables and replay the record log into them.

    ``node`` rows keep their local ids (the embedding table points at them):
    they are blanked, then every atom and tombstone record rewrites them in
    log order. Returns how many records of each type were replayed.
    """
    conn.execute("DELETE FROM link")
    conn.execute("DELETE FROM supersedes")
    conn.execute("DELETE FROM membership")
    conn.execute("DELETE FROM hub_grant")
    conn.execute("DELETE FROM share_offer")
    conn.execute("DELETE FROM share_block")
    conn.execute(
        "UPDATE node SET kind = 'unreplayed', title = NULL, text = '', tags = '{}',"
        " payload = '{}'::jsonb, ext_key = NULL, audience = 'private',"
        " tombstoned_at = NULL"
    )
    _seed_tombstoned(conn)
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM record WHERE type = ANY(%s) ORDER BY id",
            (list(STATE_TYPES),),
        )
    ]
    counts = dict.fromkeys(STATE_TYPES, 0)
    touched: dict[int, None] = {}
    for record_id in ids:
        rec = records.get_record(conn, record_id)
        if rec is None:
            continue
        counts[rec.type] += 1
        node_id = apply_record(conn, rec, notify=False)
        if node_id is not None:
            touched[node_id] = None
    for node_id in touched:
        hooks.on_node_changed(conn, node_id)
    return counts
