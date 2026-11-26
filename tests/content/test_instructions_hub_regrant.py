"""An account that left the instructions hub before it could no longer be left
gets it back at the next migrate; nobody else's `none` and no other hub change."""

from __future__ import annotations

from reticula_mcp import accounts
from reticula_mcp.content import bootstrap, hooks
from reticula_mcp.graph import api
from tests.graph_support import share


def _hub(conn) -> dict:
    system = bootstrap.system_account(conn)
    hub = bootstrap.find_shipped_node(conn, system, bootstrap.INSTRUCTIONS_HUB_SLUG)
    assert hub is not None
    return hub


def _mode(conn, hub_id: int, person_id: str) -> str | None:
    row = conn.execute(
        "SELECT mode FROM hub_grant WHERE hub_node_id = %s AND grantee = %s",
        (hub_id, person_id),
    ).fetchone()
    return row["mode"] if row else None


def _left_before_the_rule(conn, hub_id: int, person: accounts.Account) -> None:
    """What share(mode="none") on oneself wrote before it was refused."""
    api.set_grant(conn, person, hub_id, person.person_id, "none")


def test_an_account_that_left_gets_the_hub_back_at_migrate(conn, make_account):
    accounts.ensure_system_account(conn)
    hooks.on_migrated(conn)
    z = make_account()
    hub = _hub(conn)
    hooks.on_migrated(conn)
    assert _mode(conn, hub["id"], z.person_id) == "read"
    _left_before_the_rule(conn, hub["id"], z)
    assert _mode(conn, hub["id"], z.person_id) == "none"
    hooks.on_migrated(conn)
    assert _mode(conn, hub["id"], z.person_id) == "read"


def test_a_none_written_by_the_system_stays(conn, make_account):
    accounts.ensure_system_account(conn)
    hooks.on_migrated(conn)
    z = make_account()
    hub = _hub(conn)
    system = bootstrap.system_account(conn)
    api.set_grant(conn, system, hub["id"], z.person_id, "none")
    hooks.on_migrated(conn)
    assert _mode(conn, hub["id"], z.person_id) == "none"


def test_another_accounts_hub_with_the_tag_is_untouched(conn, make_account):
    accounts.ensure_system_account(conn)
    hooks.on_migrated(conn)
    owner, z = make_account(), make_account()
    fake = api.create_atom(
        conn,
        owner,
        kind="hub",
        text="fake",
        title="Reticula instructions",
        tags=["slug:hub-instructions"],
    )
    share(conn, owner, fake["address"], z, "read")
    api.set_grant(conn, z, fake["id"], z.person_id, "none")
    hooks.on_migrated(conn)
    assert _mode(conn, fake["id"], z.person_id) == "none"
