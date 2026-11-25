"""The Reticula instructions hub cannot be left: bootstrap never grants again
over an existing row, so a `none` grant would cut the account off for good."""

from __future__ import annotations

import pytest

from reticula_mcp import accounts
from reticula_mcp.content import bootstrap
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, handlers
from tests.graph_support import mk_hub, share


def instructions_hub(conn) -> str:
    accounts.ensure_system_account(conn)
    bootstrap.ensure_shipped_content(conn, api)
    row = conn.execute(
        "SELECT address FROM node WHERE kind = 'hub'"
        " AND 'slug:hub-instructions' = ANY(tags)"
    ).fetchone()
    return row["address"]


def grant_of(conn, hub: str, person_id: str) -> str | None:
    row = conn.execute(
        "SELECT g.mode FROM hub_grant g JOIN node h ON h.id = g.hub_node_id"
        " WHERE h.address = %s AND g.grantee = %s",
        (hub, person_id),
    ).fetchone()
    return row["mode"] if row else None


@pytest.mark.parametrize("who", ["handle", "person_id"])
def test_leaving_the_instructions_hub_is_refused(conn, make_account, who):
    z = make_account(handle="zed")
    hub = instructions_hub(conn)
    bootstrap.ensure_shipped_content(conn, api)  # grants z read
    assert grant_of(conn, hub, z.person_id) == "read"
    person = "@zed" if who == "handle" else z.person_id
    with pytest.raises(ReticulaError) as e:
        handlers.share(conn, z, hub=hub, person=person, mode="none")
    assert e.value.code == "Forbidden"
    assert e.value.message == "the Reticula instructions hub cannot be left"
    assert "shares nothing of yours" in (e.value.hint or "")
    assert grant_of(conn, hub, z.person_id) == "read"


def test_leaving_another_hub_still_works(conn, make_account):
    owner, z = make_account(), make_account(handle="zed")
    hub = mk_hub(conn, owner, "Club")
    share(conn, owner, hub, z, "read")
    out = handlers.share(conn, z, hub=hub, person="@zed", mode="none")
    assert "no longer has access" in out
    assert grant_of(conn, hub, z.person_id) == "none"


def test_an_imposter_hub_with_the_tag_can_be_left(conn, make_account):
    owner, z = make_account(), make_account(handle="zed")
    fake = api.create_atom(
        conn,
        owner,
        kind="hub",
        text="fake",
        title="Reticula instructions",
        tags=["slug:hub-instructions"],
    )["address"]
    share(conn, owner, fake, z, "read")
    out = handlers.share(conn, z, hub=fake, person="@zed", mode="none")
    assert "no longer has access" in out


def test_reading_and_listing_access_to_the_instructions_hub_still_work(
    conn, make_account
):
    z = make_account(handle="zed")
    hub = instructions_hub(conn)
    bootstrap.ensure_shipped_content(conn, api)
    assert handlers.share(conn, z, hub=hub).startswith("access to hub ")
