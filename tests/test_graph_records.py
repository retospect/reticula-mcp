"""Records, materialisation, rebuild, the API contract and the tool outputs."""

from __future__ import annotations

from typing import Any

import pytest

from reticula_mcp import accounts, records
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers
from reticula_mcp.graph.materialize import rebuild
from reticula_mcp.graph.paper import normalize_ext_id
from reticula_mcp.search import hooks
from tests.graph_support import addr, can_read, mk_hub, mk_note, node, readable, share


def code_of(call, *args, **kwargs) -> str:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value.code


def build_world(conn, make_account) -> dict[str, Any]:
    """A little of everything the log can hold."""
    alice, bob, carol, dan = (make_account() for _ in range(4))
    top = mk_hub(conn, alice, "Top")
    mid = mk_hub(conn, alice, "Mid", parent=top)
    other = mk_hub(conn, alice, "Other", preset="moderated")
    share(conn, alice, top, bob)
    share(conn, alice, mid, carol, "read")
    share(conn, alice, top, dan, "read")
    share(conn, alice, top, dan, "none")
    share(conn, bob, top, carol, "read")
    n1 = mk_note(conn, alice, "first", hub=top, tags=["a", "b"], title="One")
    n2 = mk_note(conn, bob, "second", hub=mid)
    handlers.edit(conn, bob, ref=n2, text="second, edited", tags=["z"])
    n3 = mk_note(conn, alice, "third", hub=top, supersedes=[n1])
    handlers.link(conn, alice, ref=n3, to=n2, rel="refutes")
    priv = mk_note(conn, bob, "bob private", hub=top, private=True)
    paper = addr(
        handlers.put(
            conn, alice, text="", kind="paper", title="P", ext_id="arXiv:2401.00001v3"
        )
    )
    handlers.link(conn, alice, ref=paper, to=other)
    gone = mk_note(conn, alice, "to be deleted", hub=top)
    handlers.edit(conn, alice, ref=gone, text="to be deleted, v2")
    handlers.delete(conn, alice, ref=gone)
    removed = mk_note(conn, bob, "removed by admin", hub=top)
    handlers.link(conn, alice, ref=removed, to=top, remove=True)
    leaver = mk_note(conn, bob, "withdrawn by author", hub=mid)
    handlers.link(conn, bob, ref=leaver, to=mid, remove=True)
    handlers.put(conn, dan, text="unfiled")
    return {
        "people": (alice, bob, carol, dan),
        "hubs": (top, mid, other),
        "notes": (n1, n2, n3, priv, paper, gone, removed, leaver),
    }


def snapshot(conn) -> dict[str, Any]:
    """Every graph table keyed by addresses, ignoring local ids."""
    nodes = {
        r["address"]: {k: v for k, v in r.items() if k not in ("id", "fts")}
        for r in conn.execute("SELECT * FROM node")
    }
    addr_of = {
        r["id"]: r["address"] for r in conn.execute("SELECT id, address FROM node")
    }
    members = {
        r["address"]: (
            addr_of[r["node_id"]],
            addr_of[r["hub_node_id"]],
            r["author"],
            r["audience"],
            r["withdrawn"],
            r["record_id"],
            r["created_at"],
        )
        for r in conn.execute("SELECT * FROM membership")
    }
    grants = {
        (addr_of[r["hub_node_id"]], r["grantee"]): (
            r["grantee_account_id"],
            r["mode"],
            r["granted_by"],
            r["address"],
            r["record_id"],
            r["created_at"],
            r["updated_at"],
        )
        for r in conn.execute("SELECT * FROM hub_grant")
    }
    sup = {
        (addr_of[r["new_node_id"]], addr_of[r["old_node_id"]])
        for r in conn.execute("SELECT * FROM supersedes")
    }
    links = {
        (
            addr_of[r["src_node_id"]],
            r["rel"],
            r["dst_address"],
            r["dst_node_id"] is None,
        )
        for r in conn.execute("SELECT * FROM link")
    }
    return {
        "node": nodes,
        "membership": members,
        "grant": grants,
        "supersedes": sup,
        "link": links,
    }


def test_rebuild_in_place_leaves_the_tables_equal(conn, make_account):
    build_world(conn, make_account)
    before = snapshot(conn)
    ids_before = {
        r["address"]: r["id"] for r in conn.execute("SELECT id, address FROM node")
    }
    counts = rebuild(conn)
    assert counts["atom"] > 10 and counts["member"] > 5 and counts["grant"] >= 5
    assert counts["tombstone"] == 1
    assert snapshot(conn) == before
    # node ids survive (the embedding table points at them)
    assert {
        r["address"]: r["id"] for r in conn.execute("SELECT id, address FROM node")
    } == ids_before


def test_rebuild_from_empty_tables_leaves_the_tables_equal(conn, make_account):
    world = build_world(conn, make_account)
    before = snapshot(conn)
    readable_before = {p.handle: readable(conn, p) for p in world["people"]}
    for table in ("link", "supersedes", "membership", "hub_grant", "node"):
        conn.execute(f"DELETE FROM {table}")
    assert not conn.execute("SELECT 1 FROM node").fetchone()
    rebuild(conn)
    assert snapshot(conn) == before
    assert {p.handle: readable(conn, p) for p in world["people"]} == readable_before
    # the tombstoned node came back without content, from its tombstone record
    gone = world["notes"][5]
    row = node(conn, gone)
    assert row["tombstoned_at"] is not None and row["text"] == ""
    assert row["kind"] == "note"


def test_rebuild_cli_registers_and_runs(fresh_db_url, monkeypatch, capsys):
    from reticula_mcp.cli import main
    from reticula_mcp.db import connect

    with connect(fresh_db_url) as c:
        a = accounts.create_account(c, "cli@example.com", "pw-correct-horse-1", None)
        api.create_atom(c, a, kind="note", text="hello")
        c.commit()
    monkeypatch.setenv("RETICULA_DATABASE_URL", fresh_db_url)
    assert main(["rebuild"]) == 0
    assert "replayed" in capsys.readouterr().out
    with connect(fresh_db_url) as c:
        assert c.execute("SELECT count(*) AS n FROM node").fetchone()["n"] == 1


def test_every_record_the_handlers_produce_verifies(conn, make_account):
    build_world(conn, make_account)
    checked = 0
    for row in conn.execute("SELECT id FROM record ORDER BY id").fetchall():
        rec = records.get_record(conn, row["id"])
        assert rec is not None
        if rec.blanked:
            continue
        assert records.check_record(conn, rec) == [], (rec.type, rec.address)
        checked += 1
    assert checked > 30
    types = {r["type"] for r in conn.execute("SELECT DISTINCT type FROM record")}
    assert {"atom", "member", "grant", "tombstone", "identity"} <= types


def test_blanked_versions_are_exactly_the_earlier_versions_of_the_tombstoned(
    conn, make_account
):
    world = build_world(conn, make_account)
    gone = world["notes"][5]
    blanked = [
        r["address"]
        for r in conn.execute("SELECT address FROM record WHERE blanked_at IS NOT NULL")
    ]
    assert blanked == [gone, gone]


def test_node_changed_hook_runs_for_insert_edit_and_tombstone(
    conn, make_account, monkeypatch
):
    seen: list[int] = []
    monkeypatch.setattr(
        hooks, "on_node_changed", lambda c, node_id: seen.append(node_id)
    )
    alice = make_account()
    n = mk_note(conn, alice, "v1")
    nid = node(conn, n)["id"]
    assert seen == [nid]
    handlers.edit(conn, alice, ref=n, text="v2")
    assert seen == [nid, nid]
    handlers.edit(conn, alice, ref=n, text="v2")  # no change: no new version
    assert seen == [nid, nid]
    handlers.delete(conn, alice, ref=n)
    assert seen == [nid, nid, nid]
    seen.clear()
    rebuild(conn)
    assert seen == [nid]  # once per node at the end of a rebuild


def test_edit_appends_a_version_with_the_head_as_only_parent(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob)
    n = mk_note(conn, bob, "v1", hub=hub, title="T")
    v1 = records.head(conn, n)
    handlers.edit(conn, bob, ref=n, text="v2")
    handlers.edit(conn, bob, ref=n, title="T2", tags=["t"])
    versions = records.versions(conn, n)
    assert len(versions) == 3
    assert versions[1].parents == (versions[0].version_hash,)
    assert versions[2].parents == (versions[1].version_hash,)
    assert versions[0].version_hash == v1.version_hash
    row = node(conn, n)
    assert (row["text"], row["title"], row["tags"]) == ("v2", "T2", ["t"])
    assert row["head_version"] == versions[2].version_hash
    assert code_of(handlers.edit, conn, alice, ref=n, text="hijack") == "Forbidden"
    assert code_of(handlers.edit, conn, bob, ref=n) == "BadInput"
    assert "unchanged" in handlers.edit(conn, bob, ref=n, text="v2")
    assert len(records.versions(conn, n)) == 3
    assert code_of(handlers.edit, conn, bob, ref=hub, preset="personal") == "Forbidden"


def test_put_input_rules(conn, make_account):
    alice = make_account()
    assert code_of(handlers.put, conn, alice, text="") == "BadInput"
    assert code_of(handlers.put, conn, alice, text="x", kind="hub") == "BadInput"
    assert code_of(handlers.put, conn, alice, text="x", preset="group") == "BadInput"
    assert (
        code_of(
            handlers.put, conn, alice, text="x", kind="hub", title="t", preset="zzz"
        )
        == "BadInput"
    )
    assert (
        code_of(handlers.put, conn, alice, text="", kind="paper", title="t")
        == "BadInput"
    )
    assert (
        code_of(handlers.put, conn, alice, text="x", supersedes=["ffffffffff"])
        == "NotFound"
    )
    note = mk_note(conn, alice, "n")
    assert code_of(handlers.put, conn, alice, text="x", hub=note) == "BadInput"
    # unknown kinds are accepted and stored
    other = addr(handlers.put(conn, alice, text="x", kind="Weird-Kind"))
    assert node(conn, other)["kind"] == "weird-kind"
    inst = addr(handlers.put(conn, alice, text="do this", kind="instruction"))
    assert node(conn, inst)["kind"] == "instruction"


def test_origin_and_anchor_are_signed_into_the_payload(conn, make_account):
    alice = make_account()
    paper = addr(
        handlers.put(conn, alice, text="", kind="paper", title="P", ext_id="doi:10.1/x")
    )
    by_agent = addr(
        handlers.put(
            conn,
            alice,
            text="about the paper",
            ext_id="doi:10.1/x",
            anchor="as shown in",
            model="claude-x",
        )
    )
    by_person = mk_note(conn, alice, "plain")
    p = records.head(conn, by_agent).payload
    assert p["origin"] == {"by": "agent", "model": "claude-x"}
    assert p["anchor"] == "as shown in" and p["ext"]["key"] == "doi:10.1/x"
    assert records.head(conn, by_person).payload["origin"] == {"by": "person"}
    # only paper stubs carry an ext_key
    assert node(conn, by_agent)["ext_key"] is None
    assert node(conn, paper)["ext_key"] == "doi:10.1/x"


@pytest.mark.parametrize(
    ("given", "key"),
    [
        ("doi:10.1000/ABC", "doi:10.1000/abc"),
        ("DOI:10.1000/ABC", "doi:10.1000/abc"),
        ("  doi:10.1000/abc ", "doi:10.1000/abc"),
        ("https://doi.org/10.1000/ABC", "doi:10.1000/abc"),
        ("doi:https://doi.org/10.1000/ABC", "doi:10.1000/abc"),
        ("http://dx.doi.org/10.1000/abc", "doi:10.1000/abc"),
        ("arxiv:2401.00001", "arxiv:2401.00001"),
        ("arXiv:2401.00001v2", "arxiv:2401.00001"),
        ("arxiv:hep-th/9901001V12", "arxiv:hep-th/9901001"),
        ("isbn:978-3-16-148410-0", "isbn:978-3-16-148410-0"),
        ("  Some Local Key ", "Some Local Key"),
    ],
)
def test_ext_id_normalisation(given, key):
    assert normalize_ext_id(given)[0] == key


@pytest.mark.parametrize("bad", ["", "   ", "doi:", "arxiv:v2"])
def test_ext_id_normalisation_rejects_empty(bad):
    with pytest.raises(ReticulaError):
        normalize_ext_id(bad)


def test_links_and_supersedes_relations(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "H")
    share(conn, alice, hub, bob)
    a = mk_note(conn, alice, "a", hub=hub)
    b = mk_note(conn, bob, "b", hub=hub)
    assert "added" in handlers.link(conn, alice, ref=a, to=b, rel="cites")
    assert "no change" in handlers.link(conn, alice, ref=a, to=b, rel="cites")
    assert conn.execute("SELECT rel, dst_address FROM link").fetchall() == [
        {"rel": "cites", "dst_address": b}
    ]
    assert "added" in handlers.link(conn, alice, ref=a, to=b, rel="supersedes")
    assert conn.execute("SELECT count(*) AS n FROM supersedes").fetchone()["n"] == 1
    assert "removed" in handlers.link(
        conn, alice, ref=a, to=b, rel="supersedes", remove=True
    )
    assert conn.execute("SELECT count(*) AS n FROM supersedes").fetchone()["n"] == 0
    assert code_of(handlers.link, conn, bob, ref=a, to=b, rel="cites") == "Forbidden"
    assert code_of(handlers.link, conn, alice, ref=a, to=a, rel="cites") == "BadInput"


def test_move_is_a_withdrawal_plus_a_new_statement(conn, make_account):
    alice, bob = make_account(), make_account()
    h1, h2 = mk_hub(conn, alice, "H1"), mk_hub(conn, alice, "H2")
    share(conn, alice, h1, bob, "read")
    share(conn, alice, h2, bob, "read")
    n = mk_note(conn, alice, "n", hub=h1)
    before = conn.execute("SELECT count(*) AS n FROM record").fetchone()["n"]
    handlers.link(conn, alice, ref=n, to=h1, remove=True)
    handlers.link(conn, alice, ref=n, to=h2)
    assert (
        conn.execute("SELECT count(*) AS n FROM record").fetchone()["n"] == before + 2
    )
    assert can_read(conn, bob, n)
    assert "already filed" in handlers.link(conn, alice, ref=n, to=h2)
    assert code_of(handlers.link, conn, alice, ref=n, to=h1, remove=True) == "NotFound"


def test_filing_requires_a_hub(conn, make_account):
    alice = make_account()
    a, b = mk_note(conn, alice, "a"), mk_note(conn, alice, "b")
    assert code_of(handlers.link, conn, alice, ref=a, to=b) == "BadInput"


# --- the internal API other lanes bind to ------------------------------------------


def test_api_contract_for_the_content_lane(conn, make_account):
    system = accounts.ensure_system_account(conn)
    alice = make_account()
    hub = api.create_atom(
        conn,
        system,
        kind="hub",
        text="x",
        title="Debug",
        tags=["slug:hub-debug"],
        payload_extra={"accept": "anyone"},
    )
    assert isinstance(hub, dict) and hub["id"] and hub["address"].startswith("ret:")
    assert hub["tags"] == ["slug:hub-debug"] and hub["payload"]["accept"] == "anyone"
    assert records.head(conn, hub["address"]).payload["accept"] == "anyone"
    note = api.create_atom(
        conn, alice, kind="debug", text="d", hub_node_id=hub["id"], model="m1"
    )
    assert records.head(conn, note["address"]).payload["origin"] == {
        "by": "agent",
        "model": "m1",
    }
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM membership WHERE node_id = %s AND hub_node_id = %s",
            (note["id"], hub["id"]),
        ).fetchone()["n"]
        == 1
    )
    personal = api.create_atom(
        conn,
        system,
        kind="hub",
        text="",
        title="Mine",
        payload_extra={"preset": "personal"},
    )
    assert personal["payload"]["preset"] == "personal"
    # update keeps given tags and writes one new version
    before = len(records.versions(conn, personal["address"]))
    updated = api.update_atom(
        conn, system, personal["id"], tags=["slug:x"], text="more"
    )
    assert updated["tags"] == ["slug:x"]
    assert len(records.versions(conn, personal["address"])) == before + 1
    # the system account grants on its own hub
    api.set_grant(conn, system, personal["id"], alice.person_id, "read")
    assert can_read(conn, alice, personal["address"])
    api.add_membership(conn, alice, note["id"], hub["id"])  # idempotent
    assert conn.execute("SELECT count(*) AS n FROM membership").fetchone()["n"] == 1
    assert code_of(api.update_atom, conn, alice, hub["id"], text="x") == "Forbidden"
    assert (
        code_of(api.add_membership, conn, alice, personal["id"], hub["id"])
        == "Forbidden"
    )
    assert (
        code_of(api.set_grant, conn, alice, hub["id"], system.person_id, "none")
        == "Forbidden"
    )
