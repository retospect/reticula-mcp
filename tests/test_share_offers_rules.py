"""Who has a hub between them, who may change a share that waits, and how many
may wait (migrations 0304, 0305). A share applies at once only after the
person it is for has said yes to the sharer before: they accepted a share of
theirs, or shared a hub with them themselves. Being able to read the hub
already, or a share through a third person, is no yes."""

from __future__ import annotations

import pytest

from reticula_mcp import account_admin
from reticula_mcp.content import handlers as content
from reticula_mcp.errors import ReticulaError
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


def offer_lines(conn, who) -> list[str]:
    return [
        ln
        for ln in content.start(conn, who).splitlines()
        if "shared a hub with you" in ln or "more shares waiting" in ln
    ]


@pytest.fixture
def people(conn, make_account):
    return [make_account(handle=h) for h in ("ada", "ben", "cyd", "dan")]


# --- who has a hub between them ---------------------------------------------


def test_a_hub_the_person_reads_through_a_hub_above_is_no_yes(conn, people):
    ada, ben, cyd, _ = people
    common = mk_hub(conn, cyd, "Common")
    share(conn, cyd, common, ada, "add")  # each said yes to cyd
    share(conn, cyd, common, ben, "read")
    under = mk_hub(conn, ada, "Under", parent=common)
    assert under in readable(conn, ben)  # through the hub above
    assert waits(offer(conn, ada, under, ben))
    other = mk_hub(conn, ada, "Other")
    assert waits(offer(conn, ada, other, ben))
    assert other not in readable(conn, ben)


def test_a_share_through_a_third_person_is_no_yes_to_the_hubs_owner(conn, people):
    ada, ben, cyd, _ = people
    team = mk_hub(conn, ada, "Team")
    share(conn, ada, team, cyd, "add")  # cyd said yes to ada
    first = mk_hub(conn, cyd, "Cyd's")
    share(conn, cyd, first, ben, "read")  # ben said yes to cyd
    assert not waits(offer(conn, cyd, team, ben))  # cyd and ben: at once
    other = mk_hub(conn, ada, "Other")
    assert waits(offer(conn, ada, other, ben))  # ada and ben: no yes yet


def test_a_yes_counts_for_the_one_who_was_answered_or_shared_with(conn, people):
    ada, ben, _, _ = people
    one = mk_hub(conn, ada, "One")
    offer(conn, ada, one, ben)
    answer(conn, ben, one, "read")  # ben said yes to ada
    # ada said yes to nobody: ben's share to ada waits
    first = mk_hub(conn, ben, "Ben's")
    assert waits(offer(conn, ben, first, ada))
    graph.share(conn, ben, hub=first, person="@ada", mode="none")  # taken back
    # ada's own share to ben, which applies at once, is ada's yes to ben
    assert not waits(offer(conn, ada, mk_hub(conn, ada, "Two"), ben))
    assert not waits(offer(conn, ben, mk_hub(conn, ben, "Ben's 2"), ada))


def test_a_rebuild_decides_from_the_tables_not_from_the_record(conn, people):
    ada, ben, _, _ = people
    hub = mk_hub(conn, ada, "Garden")
    offer(conn, ada, hub, ben)
    # a grant record of the same share that says nothing of waiting
    api._append(
        conn,
        ada,
        type="grant",
        address=api.fmt.new_address(ada.person_id),
        payload={"hub": hub, "to": ben.person_id, "mode": "add", "wrapped_key": None},
        hub_id=None,
        audience="hub",
    )
    materialize.rebuild(conn)
    assert hub not in readable(conn, ben)
    assert len(offer_lines(conn, ben)) == 1


def write_grant(conn, author, hub: str, grantee, mode: str) -> None:
    """A grant record applied as it is, past the tool's own checks: what the
    record layer does with it."""
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
        },
        hub_id=None,
        audience="hub",
    )
    materialize.apply_record(conn, rec)


def test_the_record_layer_keeps_anothers_share_and_a_refusal(conn, people):
    ada, ben, cyd, dan = people
    team = mk_hub(conn, ada, "Team", preset="group")
    for member in (cyd, dan):
        share(conn, ada, team, member, "add")
    offer(conn, cyd, team, ben, "read")
    write_grant(conn, dan, team, ben, "add")
    assert "shared by @cyd" in graph.share(conn, ada, hub=team)
    answer(conn, ben, team, "none")
    write_grant(conn, cyd, team, ben, "add")
    assert offer_lines(conn, ben) == []  # still refused


def test_an_accept_that_leaves_no_grant_is_not_reported_done(conn, people, monkeypatch):
    ada, ben, _, _ = people
    hub = mk_hub(conn, ada, "Garden")
    offer(conn, ada, hub, ben)
    monkeypatch.setattr(materialize, "_answer_offer", lambda *a, **k: None)
    with pytest.raises(ReticulaError) as exc:
        answer(conn, ben, hub, "read")
    assert exc.value.message == "that share is no longer available"


# --- who may change a share that waits -------------------------------------


def test_one_sharer_cannot_replace_anothers_share_that_waits(conn, people):
    ada, ben, cyd, dan = people
    team = mk_hub(conn, ada, "Team", preset="group")
    for member in (cyd, dan):
        share(conn, ada, team, member, "add")
    offer(conn, cyd, team, ben, "read")
    with pytest.raises(ReticulaError) as exc:
        offer(conn, dan, team, ben, "add")
    assert exc.value.code == "Forbidden"
    assert "shared by @cyd" in graph.share(conn, ada, hub=team)
    offer(conn, ada, team, ben, "add")  # the hub's admin may
    assert "shared by @ada" in graph.share(conn, ada, hub=team)


def test_the_sharer_can_take_back_their_own_share_that_waits(conn, people):
    ada, ben, cyd, _ = people
    team = mk_hub(conn, ada, "Team", preset="group")
    share(conn, ada, team, cyd, "add")
    offer(conn, cyd, team, ben)
    graph.share(conn, cyd, hub=team, person="@ben", mode="none")
    assert offer_lines(conn, ben) == []


# --- how many may wait ------------------------------------------------------


def test_at_most_20_wait_from_one_sharer_to_one_person(conn, people):
    ada, ben, _, _ = people
    for n in range(20):
        offer(conn, ada, mk_hub(conn, ada, f"H{n}"), ben)
    with pytest.raises(ReticulaError):
        offer(conn, ada, mk_hub(conn, ada, "One too many"), ben)


def test_at_most_100_wait_for_one_person(conn, make_account):
    ben = make_account(handle="ben")
    for s in range(5):
        sharer = make_account(handle=f"many-{s}")
        for n in range(20):
            offer(conn, sharer, mk_hub(conn, sharer, f"H{n}"), ben)
    late = make_account(handle="late")
    with pytest.raises(ReticulaError):
        offer(conn, late, mk_hub(conn, late, "Late"), ben)


def test_start_shows_one_line_per_sharer(conn, people):
    ada, ben, cyd, _ = people
    for n in range(3):
        offer(conn, ada, mk_hub(conn, ada, f"A{n}"), ben)
    offer(conn, cyd, mk_hub(conn, cyd, "C"), ben)
    lines = [ln for ln in offer_lines(conn, ben) if "shared a hub with you" in ln]
    assert [ln.split()[1] for ln in lines] == ["@cyd", "@ada"]
    assert lines[1].endswith("; and 2 more from @ada")


def test_a_share_again_keeps_its_place_and_a_refused_one_stays_refused(conn, people):
    ada, ben, cyd, _ = people
    first = mk_hub(conn, ada, "First")
    offer(conn, ada, first, ben)
    offer(conn, cyd, mk_hub(conn, cyd, "Later"), ben)
    offer(conn, ada, first, ben, "add")  # again, another mode
    lines = [ln for ln in offer_lines(conn, ben) if "shared a hub with you" in ln]
    assert [ln.split()[1] for ln in lines] == ["@cyd", "@ada"]
    answer(conn, ben, first, "none")

    def records() -> int:
        return conn.execute("SELECT count(*) AS n FROM record").fetchone()["n"]

    before = records()
    assert waits(offer(conn, ada, first, ben))  # the sharer is told the same
    assert records() == before  # and nothing is written
    assert all("@ada" not in ln for ln in offer_lines(conn, ben))


# --- answering --------------------------------------------------------------


def test_any_failed_accept_says_one_line(conn, people):
    ada, ben, _, _ = people
    hub = mk_hub(conn, ada, "Garden")
    offer(conn, ada, hub, ben)
    graph.edit(conn, ada, ref=hub, private=True)  # no longer shareable
    with pytest.raises(ReticulaError) as exc:
        answer(conn, ben, hub, "read")
    assert exc.value.message == "that share is no longer available"
    assert exc.value.hint is None
    assert hub not in readable(conn, ben)


def test_the_blockers_own_share_lifts_the_block_only_once_it_applies(conn, people):
    ada, ben, _, _ = people
    one, two = mk_hub(conn, ada, "One"), mk_hub(conn, ada, "Two")
    offer(conn, ada, one, ben)
    graph.share(conn, ben, hub=f"{short(one)}:block", person="@ben", mode="none")
    offer(conn, ada, two, ben)  # held
    back = mk_hub(conn, ben, "Back")
    assert waits(offer(conn, ben, back, ada))  # waits for ada: no lift yet
    assert offer_lines(conn, ben) == []
    answer(conn, ada, back, "read")  # ada's yes: now it applies, the block goes
    assert any(short(two) in ln for ln in offer_lines(conn, ben))


def test_deleting_an_account_leaves_no_share_of_it_waiting_or_blocked(conn, people):
    ada, ben, cyd, _ = people
    offer(conn, ada, mk_hub(conn, ada, "To ben"), ben)
    offer(conn, cyd, mk_hub(conn, cyd, "To ada"), ada)
    one = mk_hub(conn, cyd, "Cyd's")
    offer(conn, cyd, one, ada)
    graph.share(conn, ada, hub=f"{short(one)}:block", person="@ada", mode="none")
    account_admin._erase_residue(conn, ada, "ada@example.com")
    for table, cols in (
        ("share_offer", ("grantee", "granted_by")),
        ("share_block", ("person", "blocked")),
    ):
        n = conn.execute(
            f"SELECT count(*) AS n FROM {table}"
            f" WHERE {cols[0]} = %(p)s OR {cols[1]} = %(p)s",
            {"p": ada.person_id},
        ).fetchone()["n"]
        assert n == 0, table
