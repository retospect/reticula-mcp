"""A yes lasts as long as the access it gave (migration 0305): a change between
read and add, by anyone, keeps it; mode none on that grant ends it, and so
does deleting the account. A refusal holds against the one sharer refused.
Past 100 shares waiting for one person, a share waits held, as from someone
blocked. And the cut-off of 0305 keeps the meaning of older records."""

from __future__ import annotations

import pytest

from reticula_mcp.content import handlers as content
from reticula_mcp.graph import api, materialize
from reticula_mcp.graph import handlers as graph
from tests.graph_support import mk_hub, readable, share
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def short(address: str) -> str:
    return address[-8:]


def offer(conn, owner, hub: str, person, mode: str = "read") -> str:
    return graph.share(conn, owner, hub=hub, person=f"@{person.handle}", mode=mode)


def answer(conn, who, hub: str, mode: str) -> str:
    return graph.share(conn, who, hub=short(hub), person=f"@{who.handle}", mode=mode)


def waits(reply: str) -> bool:
    return "once they accept" in reply


def shown(conn, who) -> list[str]:
    return [
        ln
        for ln in content.start(conn, who).splitlines()
        if "shared a hub with you" in ln
    ]


@pytest.fixture
def people(conn, make_account):
    return [make_account(handle=h) for h in ("ada", "ben", "cyd", "dan")]


def write_grant(conn, author, hub: str, grantee, mode: str, **extra) -> None:
    rec = api._append(
        conn,
        author,
        type="grant",
        address=api.fmt.new_address(author.person_id),
        payload={
            "hub": hub,
            "to": grantee.person_id,
            "mode": mode,
            "wrapped_key": None,
            **extra,
        },
        hub_id=None,
        audience="hub",
    )
    materialize.apply_record(conn, rec)


# --- a yes lasts as long as the access it gave ------------------------------


def test_an_admins_change_of_mode_keeps_the_yes(conn, people):
    ada, ben, cyd, _ = people
    team = mk_hub(conn, ada, "Team", preset="group")
    share(conn, ada, team, cyd, "add")
    offer(conn, cyd, team, ben, "read")
    answer(conn, ben, team, "read")  # ben's yes to cyd
    graph.share(conn, ada, hub=team, person="@ben", mode="add")  # the admin
    assert not waits(offer(conn, cyd, mk_hub(conn, cyd, "Cyd's"), ben))


def test_leaving_ends_the_yes(conn, people):
    ada, ben, _, _ = people
    one = mk_hub(conn, ada, "One")
    offer(conn, ada, one, ben)
    answer(conn, ben, one, "read")
    graph.share(conn, ben, hub=one, person="@ben", mode="none")  # ben leaves
    assert waits(offer(conn, ada, mk_hub(conn, ada, "Two"), ben))


def test_a_change_of_mode_on_one_hub_adds_nobody_to_another(conn, people):
    ada, ben, cyd, _ = people
    team = mk_hub(conn, ada, "Team", preset="group")
    share(conn, ada, team, cyd, "add")
    offer(conn, cyd, team, ben, "read")
    answer(conn, ben, team, "read")
    # ada changes ben's mode at once (ben has a grant on this hub) ...
    assert not waits(graph.share(conn, ada, hub=team, person="@ben", mode="add"))
    # ... which is no yes of ben's to ada
    other = mk_hub(conn, ada, "Other")
    assert waits(offer(conn, ada, other, ben))
    assert other not in readable(conn, ben)


# --- the hub ways in --------------------------------------------------------


def test_a_hub_filed_under_a_hub_that_takes_anyone_still_waits(conn, people):
    ada, ben, _, _ = people
    inbox = mk_hub(conn, ben, "Ben's inbox")
    conn.execute(
        'UPDATE node SET payload = payload || \'{"accept": "anyone"}\'::jsonb'
        " WHERE address = %s",
        (inbox,),
    )
    filed = mk_hub(conn, ada, "Filed", parent=inbox)
    assert waits(offer(conn, ada, filed, ben))
    assert waits(offer(conn, ada, mk_hub(conn, ada, "Next"), ben))


def test_a_member_who_invites_gives_no_yes_to_the_hubs_owner(conn, people):
    ada, ben, cyd, _ = people
    group = mk_hub(conn, ada, "Group", preset="group")  # members invite
    share(conn, ada, group, cyd, "add")
    share(conn, cyd, mk_hub(conn, cyd, "Cyd's"), ben, "read")  # ben's yes to cyd
    assert not waits(offer(conn, cyd, group, ben, "read"))
    assert waits(offer(conn, ada, mk_hub(conn, ada, "Ada's"), ben))


# --- a refusal holds against the one sharer ---------------------------------


def test_a_refusal_holds_only_against_the_sharer_refused(conn, people):
    ada, ben, cyd, _ = people
    team = mk_hub(conn, ada, "Team", preset="group")
    share(conn, ada, team, cyd, "add")
    offer(conn, cyd, team, ben)
    answer(conn, ben, team, "none")
    assert waits(offer(conn, cyd, team, ben))  # cyd again: still refused
    assert shown(conn, ben) == []
    offer(conn, ada, team, ben)  # the admin's own share of it shows
    (line,) = shown(conn, ben)
    assert line.split()[1] == "@ada"
    # the block form acts on the share that waits, the admin's
    out = graph.share(conn, ben, hub=f"{short(team)}:block", person="@ben", mode="none")
    assert out.startswith(f"refused @ada's share of hub id {short(team)}")


def test_block_after_refuse_finds_the_refused_sharer(conn, people):
    ada, ben, _, _ = people
    hub = mk_hub(conn, ada, "Garden")
    offer(conn, ada, hub, ben)
    answer(conn, ben, hub, "none")
    out = graph.share(conn, ben, hub=f"{short(hub)}:block", person="@ben", mode="none")
    assert "was already refused" in out
    offer(conn, ada, mk_hub(conn, ada, "Second"), ben)
    assert shown(conn, ben) == []
    materialize.rebuild(conn)
    assert shown(conn, ben) == []


# --- past 100 for one person ------------------------------------------------


def test_past_100_a_share_waits_held_and_the_sharer_is_told_it_waits(
    conn, make_account
):
    ben = make_account(handle="ben")
    for s in range(5):
        sharer = make_account(handle=f"many-{s}")
        for n in range(20):
            offer(conn, sharer, mk_hub(conn, sharer, f"H{n}"), ben)
    late = make_account(handle="late")
    hub = mk_hub(conn, late, "Late")
    assert waits(offer(conn, late, hub, ben))
    assert "waiting for them to accept" in graph.share(conn, late, hub=hub)
    assert all("@late" not in ln for ln in shown(conn, ben))
    assert len(shown(conn, ben)) == 5


# --- the cut-off of 0305 ----------------------------------------------------


def test_a_record_before_the_cut_off_keeps_its_meaning(conn, people):
    ada, ben, cyd, _ = people
    conn.execute("UPDATE share_rule_start SET record_id = 9223372036854775807")
    plain = mk_hub(conn, ada, "Plain")
    write_grant(conn, ada, plain, ben, "read")  # 0.4: applies at once
    asked = mk_hub(conn, cyd, "Asked")
    write_grant(conn, cyd, asked, ben, "read", pending=True)  # 0304 alone: waits
    assert plain in readable(conn, ben) and asked not in readable(conn, ben)
    assert [ln.split()[1] for ln in shown(conn, ben)] == ["@cyd"]


def test_without_the_cut_off_every_share_waits(conn, people):
    ada, ben, _, _ = people
    one = mk_hub(conn, ada, "One")
    offer(conn, ada, one, ben)
    answer(conn, ben, one, "read")  # a yes: the next would apply at once
    conn.execute("DELETE FROM share_rule_start")
    two = mk_hub(conn, ada, "Two")
    write_grant(conn, ada, two, ben, "read")
    assert two not in readable(conn, ben)
