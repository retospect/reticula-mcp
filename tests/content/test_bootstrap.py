"""Acceptance lines 3 and 4, plus debug notes and the CLI command."""

from __future__ import annotations

import pytest

from reticula_mcp import records
from reticula_mcp.content import bootstrap, debug, hooks
from reticula_mcp.content.bootstrap import Instruction
from reticula_mcp.content.cli import register  # noqa: F401  (import check)
from tests.content.fake_graph import FakeGraph


def _node(conn, slug):
    return bootstrap.find_shipped_node(conn, bootstrap.system_account(conn), slug)


def _versions(conn, slug):
    return records.versions(conn, _node(conn, slug)["address"])


def test_loader_creates_both_hubs_and_the_instructions_once(conn):
    graph = FakeGraph()
    result = bootstrap.ensure_shipped_content(conn, graph)
    shipped = bootstrap.shipped_instructions()
    assert sorted(result.created) == sorted(
        [bootstrap.INSTRUCTIONS_HUB_SLUG, bootstrap.DEBUG_HUB_SLUG]
        + [i.slug for i in shipped]
    )
    system = bootstrap.system_account(conn)
    assert system is not None and system.is_system
    instructions_hub = _node(conn, bootstrap.INSTRUCTIONS_HUB_SLUG)
    debug_hub = _node(conn, bootstrap.DEBUG_HUB_SLUG)
    assert instructions_hub["kind"] == debug_hub["kind"] == "hub"
    # the debug hub accepts adds from anyone and is granted to nobody
    assert records.head(conn, debug_hub["address"]).payload["accept"] == "anyone"
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM hub_grant WHERE hub_node_id = %s",
            (debug_hub["id"],),
        ).fetchone()["n"]
        == 0
    )
    # every shipped text is an instruction atom signed by the system account,
    # filed under the instructions hub
    for item in shipped:
        node = _node(conn, item.slug)
        assert node["kind"] == "instruction" and node["author"] == system.person_id
        assert node["text"] == item.text and node["title"] == item.title
        head = records.head(conn, node["address"])
        assert head.author == system.person_id and records.verify_record(conn, head)
        filed = conn.execute(
            "SELECT 1 FROM membership WHERE node_id = %s AND hub_node_id = %s",
            (node["id"], instructions_hub["id"]),
        ).fetchone()
        assert filed is not None


def test_loader_is_idempotent_and_versions_only_changed_files(conn):
    graph = FakeGraph()
    bootstrap.ensure_shipped_content(conn, graph)
    records_after_first = conn.execute("SELECT count(*) AS n FROM record").fetchone()

    again = bootstrap.ensure_shipped_content(conn, graph)
    assert again.created == [] and again.updated == [] and again.granted == []
    assert conn.execute("SELECT count(*) AS n FROM record").fetchone() == (
        records_after_first
    )
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM node WHERE tags && ARRAY[%s]::text[]",
            ("slug:hub-debug",),
        ).fetchone()["n"]
        == 1
    )

    # one file changes: exactly that atom gets a second version
    shipped = bootstrap.shipped_instructions()
    changed = [
        Instruction(i.slug, i.title, i.text + "One more line.\n")
        if i.slug == "howto-share-a-hub"
        else i
        for i in shipped
    ]
    third = bootstrap.ensure_shipped_content(conn, graph, instructions=changed)
    assert third.updated == ["howto-share-a-hub"] and third.created == []
    for item in shipped:
        expected = 2 if item.slug == "howto-share-a-hub" else 1
        assert len(_versions(conn, item.slug)) == expected, item.slug
    node = _node(conn, "howto-share-a-hub")
    assert node["text"].endswith("One more line.\n")
    versions = _versions(conn, "howto-share-a-hub")
    assert versions[1].parents == (versions[0].version_hash,)

    # a changed title alone also counts; an identical rerun does nothing
    retitled = [
        Instruction(i.slug, i.title + "!", i.text) if i.slug == "starter" else i
        for i in changed
    ]
    assert bootstrap.ensure_shipped_content(
        conn, graph, instructions=retitled
    ).updated == ["starter"]
    assert (
        bootstrap.ensure_shipped_content(conn, graph, instructions=retitled).updated
        == []
    )


def test_a_new_account_gets_read_access_to_the_instructions_hub(
    conn, make_account, monkeypatch
):
    graph = FakeGraph()
    monkeypatch.setattr(hooks, "binding", graph)
    hooks.on_migrated(conn)
    hub = _node(conn, bootstrap.INSTRUCTIONS_HUB_SLUG)
    assert hub is not None

    newcomer = make_account(handle="newcomer")  # create_account calls the hook
    grant = conn.execute(
        "SELECT mode, granted_by, grantee_account_id FROM hub_grant"
        " WHERE hub_node_id = %s AND grantee = %s",
        (hub["id"], newcomer.person_id),
    ).fetchone()
    assert grant["mode"] == "read"
    assert grant["grantee_account_id"] == newcomer.id
    assert grant["granted_by"] == bootstrap.system_account(conn).person_id
    # the debug hub is not given to anyone
    debug_hub = _node(conn, bootstrap.DEBUG_HUB_SLUG)
    assert (
        conn.execute(
            "SELECT 1 FROM hub_grant WHERE hub_node_id = %s", (debug_hub["id"],)
        ).fetchone()
        is None
    )


def test_migrate_catches_up_accounts_created_before_the_hub_existed(
    conn, make_account, monkeypatch
):
    graph = FakeGraph()
    monkeypatch.setattr(hooks, "binding", graph)
    early = make_account(handle="early")  # no hub yet: the hook does nothing
    assert graph.calls == []
    hooks.on_migrated(conn)
    hub = _node(conn, bootstrap.INSTRUCTIONS_HUB_SLUG)
    row = conn.execute(
        "SELECT mode FROM hub_grant WHERE hub_node_id = %s AND grantee = %s",
        (hub["id"], early.person_id),
    ).fetchone()
    assert row["mode"] == "read"


def test_hooks_do_nothing_without_a_graph_api(conn, make_account, monkeypatch):
    # seed the hubs with the in-test graph, then take the graph away
    bootstrap.ensure_shipped_content(conn, FakeGraph())
    before = conn.execute("SELECT count(*) AS n FROM record").fetchone()
    monkeypatch.setattr(hooks, "binding", None)
    monkeypatch.setattr(hooks, "_import_graph_api", lambda: None)
    assert hooks.graph_writer() is None

    hooks.on_migrated(conn)
    account = make_account(handle="alone")
    hooks.on_account_created(conn, account)
    hub = _node(conn, bootstrap.INSTRUCTIONS_HUB_SLUG)
    assert (
        conn.execute(
            "SELECT 1 FROM hub_grant WHERE hub_node_id = %s AND grantee = %s",
            (hub["id"], account.person_id),
        ).fetchone()
        is None
    )
    # the account's own identity record is the only new log entry
    after = conn.execute("SELECT count(*) AS n FROM record").fetchone()
    assert after["n"] == before["n"] + 1


def test_hooks_use_the_graph_api_module_when_it_exists(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(hooks, "binding", None)
    monkeypatch.setattr(hooks.importlib, "import_module", lambda name: sentinel)
    assert hooks._import_graph_api() is sentinel
    assert hooks.graph_writer() is sentinel

    def missing(name):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(hooks.importlib, "import_module", missing)
    assert hooks._import_graph_api() is None

    def broken(name):
        raise ModuleNotFoundError(name="some_other_module")

    monkeypatch.setattr(hooks.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        hooks._import_graph_api()


def test_a_failing_grant_does_not_block_the_sign_up(conn, make_account, monkeypatch):
    class Broken(FakeGraph):
        def set_grant(self, *args, **kwargs):
            raise RuntimeError("boom")

    good = FakeGraph()
    bootstrap.ensure_shipped_content(conn, good)
    monkeypatch.setattr(hooks, "binding", Broken())
    account = make_account(handle="unlucky")
    assert account.handle == "unlucky"


def test_debug_notes_list_author_model_and_text_for_the_operator(conn, make_account):
    graph = FakeGraph()
    bootstrap.ensure_shipped_content(conn, graph)
    hub = _node(conn, bootstrap.DEBUG_HUB_SLUG)
    ann = make_account(handle="ann")
    ben = make_account(handle="ben")
    graph.create_atom(
        conn, ann, kind="debug", text="search returned nothing", title="empty",
        hub_node_id=hub["id"], model="model-a",
    )  # fmt: skip
    graph.create_atom(
        conn,
        ben,
        kind="debug",
        text="get failed",
        hub_node_id=hub["id"],
        model="model-b",
    )
    graph.create_atom(
        conn, ben, kind="debug", text="private", hub_node_id=hub["id"], private=True
    )
    graph.create_atom(conn, ann, kind="debug", text="not in the debug hub")
    graph.create_atom(conn, ann, kind="note", text="a note", hub_node_id=hub["id"])

    notes = debug.list_debug_notes(conn, 10)
    assert [n.text for n in notes] == ["get failed", "search returned nothing"]
    newest, oldest = notes
    assert (newest.author_handle, newest.model, newest.by) == (
        "ben",
        "model-b",
        "agent",
    )
    assert (oldest.author_handle, oldest.model, oldest.title) == (
        "ann", "model-a", "empty",
    )  # fmt: skip
    assert len(debug.list_debug_notes(conn, 1)) == 1
    text = debug.format_debug_notes(notes)
    assert "@ben" in text and "model: model-b" in text and "get failed" in text


def test_debug_notes_before_the_first_migrate_are_empty(conn):
    assert debug.list_debug_notes(conn, 5) == []
    assert debug.format_debug_notes([]) == "no debug notes"


def test_debug_notes_command_prints_them(fresh_db_url, monkeypatch, capsys):
    from reticula_mcp import accounts
    from reticula_mcp.cli import main
    from reticula_mcp.db import connect
    from tests.conftest import PASSWORD

    graph = FakeGraph()
    with connect(fresh_db_url) as c:
        bootstrap.ensure_shipped_content(c, graph)
        author = accounts.create_account(c, "dev@example.com", PASSWORD, "dev")
        hub = _node(c, bootstrap.DEBUG_HUB_SLUG)
        graph.create_atom(
            c, author, kind="debug", text="edit lost my title", hub_node_id=hub["id"],
            model="some-model",
        )  # fmt: skip
        c.commit()
    monkeypatch.setenv("RETICULA_DATABASE_URL", fresh_db_url)
    assert main(["debug-notes", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "@dev" in out and "model: some-model" in out and "edit lost my title" in out
