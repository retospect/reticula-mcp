"""Acceptance lines 1 and 2: `start` and `profile`."""

from __future__ import annotations

from reticula_mcp import accounts, records
from reticula_mcp.content import bootstrap
from reticula_mcp.content.handlers import profile, short_id, start
from tests.content.fake_graph import FakeGraph, ensure_shared_visibility


def test_start_gives_a_fresh_account_the_starter_instruction_and_an_empty_graph_hint(
    conn, make_account
):
    me = make_account(handle="newcomer")
    out = start(conn, me)
    lines = out.splitlines()
    assert lines[0].startswith("You are @newcomer")
    assert me.person_id in lines[0]
    assert "verified: no" in lines[0]
    # the starter instruction itself, from the shipped file
    starter = bootstrap.starter_instruction()
    assert starter.text.strip() in out
    assert "Reticula is a shared memory graph" in out
    # nothing saved yet: say so and say what to do first
    assert "Your hubs: none yet." in out
    assert "Nothing is saved on this account yet" in out
    assert "put(text=..., title=...)" in out
    assert len(lines) <= 60
    assert out.splitlines()[-1].startswith("next:")


def test_start_lists_own_and_shared_hubs_with_modes(conn, make_account):
    alice = make_account(handle="alice")
    bob = make_account(handle="bob")
    ensure_shared_visibility(conn, alice, bob)
    graph = FakeGraph()
    alpha = graph.create_atom(conn, alice, kind="hub", text="a", title="Alpha")
    beta = graph.create_atom(conn, bob, kind="hub", text="b", title="Beta")
    graph.set_grant(conn, alice, alpha["id"], bob.person_id, "add")
    graph.create_atom(conn, alice, kind="note", text="shared", hub_node_id=alpha["id"])
    graph.create_atom(
        conn, alice, kind="note", text="mine", hub_node_id=alpha["id"], private=True
    )
    graph.create_atom(conn, bob, kind="note", text="b1", hub_node_id=beta["id"])

    out = start(conn, bob)
    assert "Your hubs (2):" in out
    beta_line = next(line for line in out.splitlines() if line.startswith("- Beta"))
    assert f"id {short_id(beta['address'])}" in beta_line
    assert "owner" in beta_line and "1 person, 1 note" in beta_line
    alpha_line = next(line for line in out.splitlines() if line.startswith("- Alpha"))
    assert f"id {short_id(alpha['address'])}" in alpha_line
    assert " add " in alpha_line
    # two people hold Alpha (owner and bob); the private note is not counted
    assert "2 people, 1 note" in alpha_line
    assert "Nothing is saved" not in out

    # the other side sees its own hub as owner
    out_alice = start(conn, alice)
    assert "- Alpha" in out_alice and "owner" in out_alice
    assert "- Beta" not in out_alice


def test_start_names_other_instructions_and_the_debug_hub(conn, make_account):
    me = make_account(handle="reader")
    other = make_account(handle="other")
    ensure_shared_visibility(conn, other, me)  # stand-in while readable_nodes is a stub
    graph = FakeGraph()
    bootstrap.ensure_shipped_content(conn, graph)
    out = start(conn, me)
    for item in bootstrap.shipped_instructions():
        if item.slug != bootstrap.STARTER_SLUG:
            assert item.title in out
    debug_hub = bootstrap.find_shipped_node(
        conn, bootstrap.system_account(conn), bootstrap.DEBUG_HUB_SLUG
    )
    assert f"Debug hub for problem reports: id {short_id(debug_hub['address'])}" in out
    assert len(out.splitlines()) <= 60


def test_profile_shows_identity_without_key_material(conn, make_account):
    me = make_account(handle="shown")
    out = profile(conn, me)
    assert out.splitlines()[0] == "@shown"
    assert f"person: {me.person_id}" in out
    assert "verified: no" in out
    assert f"created: {me.created_at:%Y-%m-%d}" in out
    for key_id in ("root", "k1", "e1"):
        assert f"- {key_id}:" in out
    assert "ed25519" in out and "x25519" in out
    secret = conn.execute(
        "SELECT private_key FROM account_key WHERE account_id = %s AND key_id = 'k1'",
        (me.id,),
    ).fetchone()["private_key"]
    assert bytes(secret).hex() not in out


def test_profile_renames_the_handle_and_keeps_ids_and_addresses(conn, make_account):
    me = make_account(handle="before")
    graph = FakeGraph()
    note = graph.create_atom(conn, me, kind="note", text="stays put", title="t")
    versions_before = len(records.versions(conn, me.identity_address))
    assert versions_before == 1

    out = profile(conn, me, handle="After")
    assert "Renamed @before to @after" in out
    assert "person ID did not change" in out
    assert me.person_id in out

    renamed = accounts.get_account(conn, me.id)
    assert renamed.handle == "after"
    assert renamed.person_id == me.person_id
    assert renamed.identity_address == me.identity_address
    # the identity record gained a version carrying the new handle
    versions = records.versions(conn, me.identity_address)
    assert len(versions) == versions_before + 1
    assert versions[-1].payload["handle"] == "after"
    assert versions[-1].parents == (versions[0].version_hash,)
    # the existing note kept its address and is still verifiable
    row = conn.execute(
        "SELECT address FROM node WHERE id = %s", (note["id"],)
    ).fetchone()
    assert row["address"] == note["address"]
    assert records.verify_record(conn, records.head(conn, note["address"]))
    # and the profile now shows the new handle
    assert profile(conn, renamed).splitlines()[0] == "@after"


def test_profile_rename_to_a_taken_handle_is_refused(conn, make_account):
    import pytest

    from reticula_mcp.errors import ReticulaError

    me = make_account(handle="first")
    make_account(handle="second")
    with pytest.raises(ReticulaError) as caught:
        profile(conn, me, handle="second")
    assert caught.value.code == "Conflict"
    assert profile(conn, me, handle="first").startswith("Handle is already @first")
