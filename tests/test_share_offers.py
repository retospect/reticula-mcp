"""A share between two people who have no hub between them yet waits until
the person it is for accepts it (migration 0304, graph.handlers.share). Until
then nothing of the hub reaches them: not its title, not its notes, in no
start, search or get. start names the sharer and the hub's id, and how to
accept, refuse, or refuse and block. Only a direct share either way counts as
a hub between them; a third person's hub that both read does not."""

from __future__ import annotations

import pytest

from reticula_mcp.content import handlers as content
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import api, materialize
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers as search
from tests.graph_support import mk_hub, mk_note, readable
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def short(address: str) -> str:
    return address[-8:]


def offer(conn, owner, hub: str, person, mode: str = "read") -> str:
    """The sharer's call, as the tool makes it (no accept)."""
    return graph.share(conn, owner, hub=hub, person=f"@{person.handle}", mode=mode)


def answer(conn, who, hub: str, mode: str) -> str:
    return graph.share(conn, who, hub=short(hub), person=f"@{who.handle}", mode=mode)


def offer_lines(conn, who) -> list[str]:
    out = content.start(conn, who)
    return [ln for ln in out.splitlines() if "shared a hub with you" in ln]


def sees_nothing_of(conn, who, hub: str, note: str, words: str) -> None:
    out = content.start(conn, who)
    assert "Secret garden" not in out
    assert hub not in readable(conn, who) and note not in readable(conn, who)
    found = search.search(conn, who, query=words)
    assert short(note) not in found and "Secret garden" not in found
    with pytest.raises(ReticulaError) as exc:
        search.get(conn, who, ref=note)
    assert exc.value.code == "NotFound"
    # the hub's id answers with start's line while a share waits, else NotFound
    try:
        got = search.get(conn, who, ref=hub)
    except ReticulaError as exc:
        assert exc.code == "NotFound"
    else:
        assert got in offer_lines(conn, who) and "Secret garden" not in got


@pytest.fixture
def pair(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(conn, ada, "Secret garden")
    note = mk_note(conn, ada, "tomatoes under glass", title="Greenhouse", hub=hub)
    return ada, ben, hub, note


def test_a_share_to_a_stranger_waits_and_shows_nothing(conn, pair):
    ada, ben, hub, note = pair
    out = offer(conn, ada, hub, ben)
    assert out.startswith("@ben can read in ") and "once they accept" in out
    sees_nothing_of(conn, ben, hub, note, "tomatoes greenhouse")
    # one line: the sharer, the hub's id and the calls; the wording is pinned
    # in test_share_offer_texts.py
    (line,) = offer_lines(conn, ben)
    ref = short(hub)
    assert "@ada" in line and f"id {ref}" in line and "Secret garden" not in line
    assert f'share(hub="{ref}", person="@ben", mode="read")' in line
    assert f'share(hub="{ref}:block", person="@ben", mode="none")' in line
    listing = graph.share(conn, ada, hub=hub)
    assert "@ben read, waiting" in listing


def test_accepting_gives_the_hub_at_most_as_offered(conn, pair):
    ada, ben, hub, note = pair
    offer(conn, ada, hub, ben, "read")
    out = answer(conn, ben, hub, "add")  # asks for more than was offered
    assert out.startswith("you can now read in ") and "shared by @ada" in out
    assert "Secret garden" in out
    assert note in readable(conn, ben)
    assert "tomatoes under glass" in search.get(conn, ben, ref=note)
    assert offer_lines(conn, ben) == []
    with pytest.raises(ReticulaError):
        mk_note(conn, ben, "ben's", hub=hub)  # read only


def test_refusing_removes_it_and_a_new_share_waits_again(conn, pair):
    ada, ben, hub, note = pair
    offer(conn, ada, hub, ben)
    out = answer(conn, ben, hub, "none")
    assert out.startswith(f"refused @ada's share of hub id {short(hub)}")
    sees_nothing_of(conn, ben, hub, note, "tomatoes greenhouse")
    assert offer_lines(conn, ben) == []
    assert "@ben" not in graph.share(conn, ada, hub=hub)
    offer(conn, ada, hub, ben)  # refusing does not block
    assert len(offer_lines(conn, ben)) == 1


def test_block_holds_later_shares_silently_until_the_blocker_shares(
    conn, pair, make_account
):
    ada, ben, hub, note = pair
    offer(conn, ada, hub, ben)
    out = graph.share(conn, ben, hub=f"{short(hub)}:block", person="@ben", mode="none")
    assert "stopped further shares from @ada" in out
    second = mk_hub(conn, ada, "Second")
    reply = offer(conn, ada, second, ben)
    assert "once they accept" in reply  # the sharer is told nothing else
    assert "waiting for them to accept" in graph.share(conn, ada, hub=second)
    assert offer_lines(conn, ben) == []
    assert second not in readable(conn, ben)
    with pytest.raises(ReticulaError):  # a held share cannot be accepted
        answer(conn, ben, second, "read")
    # ben shares a hub with ada: the block is lifted, the held share shows
    own = mk_hub(conn, ben, "Ben's")
    offer(conn, ben, own, ada)
    assert [short(second) in ln for ln in offer_lines(conn, ben)] == [True]


def test_after_one_accepted_share_the_next_ones_apply_at_once_both_ways(conn, pair):
    ada, ben, hub, _ = pair
    offer(conn, ada, hub, ben)
    answer(conn, ben, hub, "read")
    second = mk_hub(conn, ada, "Second")
    assert "once they accept" not in offer(conn, ada, second, ben)
    assert second in readable(conn, ben)
    back = mk_hub(conn, ben, "Back")
    assert "once they accept" not in offer(conn, ben, back, ada)
    assert back in readable(conn, ada)


def test_a_third_persons_hub_does_not_count(conn, make_account):
    ada, ben, cyd = (make_account(handle=h) for h in ("ada", "ben", "cyd"))
    common = mk_hub(conn, cyd, "Common")
    for who in (ada, ben):
        offer(conn, cyd, common, who, "add")
        answer(conn, who, common, "add")
    own = mk_hub(conn, ada, "Ada's")
    assert "once they accept" in offer(conn, ada, own, ben)


def test_the_sharer_can_take_back_a_share_that_waits(conn, pair):
    ada, ben, hub, _ = pair
    offer(conn, ada, hub, ben)
    graph.share(conn, ada, hub=hub, person="@ben", mode="none")
    assert offer_lines(conn, ben) == []
    rows = conn.execute(
        "SELECT count(*) AS n FROM hub_grant g JOIN node h ON h.id = g.hub_node_id"
        " WHERE h.address = %s AND g.grantee = %s",
        (hub, ben.person_id),
    ).fetchone()["n"]
    assert rows == 0  # no revoked row was left for ben
    offer(conn, ada, hub, ben)  # and it can be offered again
    assert len(offer_lines(conn, ben)) == 1


def test_only_the_person_it_is_for_can_answer_it(conn, pair, make_account):
    ada, ben, hub, note = pair
    cyd = make_account(handle="cyd")
    offer(conn, ada, hub, ben)
    for who, person in ((cyd, "@cyd"), (cyd, "@ben"), (ben, "@ada")):
        with pytest.raises(ReticulaError) as exc:
            graph.share(conn, who, hub=short(hub), person=person, mode="read")
        assert exc.value.code == "NotFound"
    other = mk_hub(conn, ada, "Not offered")
    with pytest.raises(ReticulaError) as exc:
        answer(conn, ben, other, "read")
    assert exc.value.code == "NotFound"
    assert note not in readable(conn, cyd) and note not in readable(conn, ben)
    assert len(offer_lines(conn, ben)) == 1


def test_a_block_also_holds_the_other_shares_that_already_wait(conn, pair):
    ada, ben, hub, _ = pair
    other = mk_hub(conn, ada, "Other")
    offer(conn, ada, hub, ben)
    offer(conn, ada, other, ben)
    graph.share(conn, ben, hub=f"{short(hub)}:block", person="@ben", mode="none")
    assert offer_lines(conn, ben) == []
    assert "waiting" in graph.share(conn, ada, hub=other)  # silent to ada


def test_the_record_layer_refuses_what_the_tool_never_offers(conn, pair, make_account):
    """Below the tool: an answer by someone else, and an answer to a held
    share, give no access, whoever writes the record."""
    ada, ben, hub, note = pair
    cyd = make_account(handle="cyd")
    offer(conn, ada, hub, ben)
    row = conn.execute(
        "SELECT o.* FROM share_offer o JOIN node h ON h.id = o.hub_node_id"
        " WHERE h.address = %s",
        (hub,),
    ).fetchone()
    with pytest.raises(ReticulaError) as exc:
        api.answer_offer(conn, cyd, row, "read")
    assert exc.value.code == "NotFound"
    conn.execute(
        "UPDATE share_offer SET held = true WHERE address = %s", (row["address"],)
    )
    api.answer_offer(conn, ben, row, "read")
    assert note not in readable(conn, ben)
    # a refused share is not accepted afterwards either
    conn.execute(
        "UPDATE share_offer SET held = false, refused = true WHERE address = %s",
        (row["address"],),
    )
    api.answer_offer(conn, ben, row, "read")
    assert note not in readable(conn, ben)


def test_block_after_refuse_still_blocks_the_sharer(conn, pair):
    """Scenario 47: refused first, then asked to stop further shares."""
    ada, ben, hub, _ = pair
    offer(conn, ada, hub, ben)
    answer(conn, ben, hub, "none")
    out = graph.share(conn, ben, hub=f"{short(hub)}:block", person="@ben", mode="none")
    assert out == (
        f"stopped further shares from @ada; their share of hub id {short(hub)} was"
        " already refused; sharing a hub with @ada lifts this"
    )
    second = mk_hub(conn, ada, "Second")
    assert "once they accept" in offer(conn, ada, second, ben)
    assert offer_lines(conn, ben) == []  # held
    assert "waiting" in graph.share(conn, ada, hub=second)  # silent to ada
    # a refused share is never accepted, and is not listed to anyone
    with pytest.raises(ReticulaError):
        answer(conn, ben, hub, "read")
    assert "@ben" not in graph.share(conn, ada, hub=hub)
    # a block of a block answers the same, and a rebuild keeps it
    again = graph.share(
        conn, ben, hub=f"{short(hub)}:block", person="@ben", mode="none"
    )
    assert again == out
    materialize.rebuild(conn)
    assert offer_lines(conn, ben) == []


def test_a_refused_share_can_be_offered_again(conn, pair):
    ada, ben, hub, _ = pair
    offer(conn, ada, hub, ben)
    answer(conn, ben, hub, "none")
    offer(conn, ada, hub, ben)
    assert len(offer_lines(conn, ben)) == 1
    answer(conn, ben, hub, "read")
    assert hub in readable(conn, ben)


def test_get_of_a_waiting_hub_answers_with_its_start_line(conn, pair):
    ada, ben, hub, note = pair
    offer(conn, ada, hub, ben)
    (line,) = offer_lines(conn, ben)
    for ref in (short(hub), hub):
        out = search.get(conn, ben, ref=ref)
        assert out == line
        assert "Secret garden" not in out
    # a note in that hub stays unknown, and so does the hub to anyone else
    with pytest.raises(ReticulaError) as exc:
        search.get(conn, ben, ref=short(note))
    assert exc.value.code == "NotFound"
    answer(conn, ben, hub, "none")  # refused: unknown again
    with pytest.raises(ReticulaError) as exc:
        search.get(conn, ben, ref=short(hub))
    assert exc.value.code == "NotFound"


def test_block_needs_a_share_that_waits(conn, pair):
    ada, ben, hub, _ = pair
    with pytest.raises(ReticulaError) as exc:
        graph.share(conn, ben, hub=f"{short(hub)}:block", person="@ben", mode="none")
    assert exc.value.code == "NotFound"


def test_start_names_at_most_five_of_fifty(conn, make_account):
    ben = make_account(handle="ben")
    for n in range(50):
        sharer = make_account(handle=f"sharer-{n:02d}")
        offer(conn, sharer, mk_hub(conn, sharer, f"Hub {n}"), ben)
    lines = offer_lines(conn, ben)
    assert len(lines) == 5
    out = content.start(conn, ben)
    assert "- and 45 more shares waiting; the next ones show here once these are" in out
    assert "Hub " not in out


def test_a_rebuild_from_the_log_gives_the_same_offers_grants_and_blocks(
    conn, make_account
):
    ada, ben, cyd = (make_account(handle=h) for h in ("ada", "ben", "cyd"))
    accepted, waiting, blocked = (mk_hub(conn, ada, t) for t in ("A", "W", "B"))
    offer(conn, ada, accepted, ben)
    answer(conn, ben, accepted, "read")
    offer(conn, ada, waiting, cyd)
    offer(conn, ada, blocked, cyd, "add")
    graph.share(conn, cyd, hub=f"{short(waiting)}:block", person="@cyd", mode="none")

    def state():
        return (
            conn.execute(
                "SELECT hub_node_id, grantee, mode, granted_by, held FROM share_offer"
                " ORDER BY 1, 2"
            ).fetchall(),
            conn.execute(
                "SELECT person, blocked FROM share_block ORDER BY 1, 2"
            ).fetchall(),
            conn.execute(
                "SELECT hub_node_id, grantee, mode, granted_by FROM hub_grant"
                " ORDER BY 1, 2"
            ).fetchall(),
        )

    before = state()
    assert before[0] and before[1]  # one held offer, one block
    materialize.rebuild(conn)
    assert state() == before
