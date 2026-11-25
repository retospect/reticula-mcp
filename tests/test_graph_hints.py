"""Guidance in tool answers, and the wider paper-id normalisation."""

from __future__ import annotations

import pytest

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.db.migrate import migrations_dir
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers
from reticula_mcp.graph.materialize import apply_record, rebuild
from reticula_mcp.graph.paper import normalize_ext_id
from tests.graph_support import addr, can_read, mk_hub, mk_note, node, share

DOI = "doi:10.1038/s41586-020-2649-2"


def err(call, *args, **kwargs) -> ReticulaError:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value


def paper(conn, who, ext_id, title="P") -> str:
    return handlers.put(conn, who, text="", kind="paper", title=title, ext_id=ext_id)


def papers(conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM node WHERE kind = 'paper'")
    return int(row.fetchone()["n"])


def test_sharing_a_private_hub_says_how_to_go_on(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Mine")
    handlers.edit(conn, alice, ref=hub, private=True)
    e = err(share, conn, alice, hub, bob)
    assert e.code == "BadInput" and e.hint
    assert f"edit(ref={hub[-8:]}, private=false)" in e.hint
    handlers.edit(conn, alice, ref=hub, private=False)
    share(conn, alice, hub, bob)  # and then it works


def test_share_reports_the_callers_own_private_notes_in_the_hub(conn, make_account):
    alice, bob, carol = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "Team")
    sub = mk_hub(conn, alice, "Sub", parent=hub)
    share(conn, alice, hub, bob)
    assert "private" not in share(conn, alice, hub, carol, "read")  # none yet
    p1 = mk_note(conn, alice, "mine one", hub=hub, private=True)
    mk_note(conn, alice, "mine two", hub=sub, private=True)
    mk_note(conn, alice, "public", hub=hub)
    mk_note(conn, alice, "private elsewhere", private=True)  # not in this hub
    for i in range(3):  # bob's private notes are never counted or mentioned
        mk_note(conn, bob, f"bob private {i}", hub=hub, private=True)
    dan = make_account()
    out = share(conn, alice, hub, dan, "read")
    assert "note: 2 of your notes in this hub are private" in out
    assert "edit(ref=" in out and "private=false" in out
    out_b = share(conn, bob, hub, make_account(), "read")
    assert "note: 3 of your notes" in out_b
    # making one public lowers the count; revoking says nothing about notes
    handlers.edit(conn, alice, ref=p1, private=False)
    assert "note: 1 of your notes" in share(conn, alice, hub, make_account())
    assert "note:" not in share(conn, alice, hub, dan, "none")


def test_personal_hub_share_names_the_call_that_lets_members_add(conn, make_account):
    alice, bob = make_account(), make_account()
    hub = mk_hub(conn, alice, "Diary", preset="personal")
    out = share(conn, alice, hub, bob, "add")
    assert "only the owner adds in a personal hub" in out
    assert f'edit(ref={hub[-8:]}, preset="group")' in out
    assert "read" in out
    plain = share(conn, alice, hub, make_account(), "read")
    assert "only the owner adds" not in plain


def test_correction_of_a_hub_note_without_a_hub_points_at_the_link_call(
    conn, make_account
):
    alice, bob, carol = (make_account() for _ in range(3))
    hub = mk_hub(conn, alice, "Team")
    share(conn, alice, hub, bob, "add")
    share(conn, alice, hub, carol, "read")
    original = mk_note(conn, alice, "wrong fact", hub=hub)
    out = handlers.put(conn, bob, text="right fact", supersedes=[original])
    new = addr(out.splitlines()[0])
    assert f"link(ref='{new[-8:]}', to='{hub[-8:]}')" in out
    assert not can_read(conn, alice, new)  # not filed automatically
    # a reader who cannot add gets no such suggestion
    out_c = handlers.put(conn, carol, text="also right", supersedes=[original])
    assert "only you can read this" not in out_c and hub[-8:] not in out_c
    # filing it in the hub in the same call needs no hint
    out2 = handlers.put(conn, bob, text="again", supersedes=[original], hub=hub)
    assert "link(ref=" not in out2
    # following the hint makes it readable for the author
    handlers.link(conn, bob, ref=new, to=hub)
    assert can_read(conn, alice, new)
    # a private correction is not pushed into a hub
    out3 = handlers.put(
        conn, bob, text="private fix", supersedes=[original], private=True
    )
    assert "link(ref=" not in out3


@pytest.mark.parametrize(
    "form",
    [
        DOI,
        "10.1038/s41586-020-2649-2",
        "10.1038/S41586-020-2649-2",
        "DOI:10.1038/S41586-020-2649-2",
        "https://doi.org/10.1038/s41586-020-2649-2",
        "http://dx.doi.org/10.1038/S41586-020-2649-2",
        "doi.org/10.1038/s41586-020-2649-2",
        " doi:https://doi.org/10.1038/s41586-020-2649-2 ",
    ],
)
def test_every_spelling_of_a_doi_resolves_to_one_paper(conn, make_account, form):
    alice = make_account()
    first = addr(paper(conn, alice, DOI))
    again = paper(conn, alice, form, title="P2")
    assert addr(again) == first
    assert node(conn, first)["ext_key"] == DOI
    assert papers(conn) == 1


@pytest.mark.parametrize(
    "form",
    [
        "arXiv:2101.00001",
        "arxiv:2101.00001v2",
        "ARXIV:2101.00001V3",
        "2101.00001",
        "2101.00001v2",
        "https://arxiv.org/abs/2101.00001",
        "https://arxiv.org/abs/2101.00001v4",
        "http://www.arxiv.org/pdf/2101.00001v2.pdf",
        "arxiv.org/abs/2101.00001",
    ],
)
def test_every_spelling_of_an_arxiv_id_resolves_to_one_paper(conn, make_account, form):
    alice = make_account()
    first = addr(paper(conn, alice, "arXiv:2101.00001"))
    again = paper(conn, alice, form, title="P2")
    assert addr(again) == first
    assert node(conn, first)["ext_key"] == "arxiv:2101.00001"
    assert papers(conn) == 1


@pytest.mark.parametrize(
    ("given", "key"),
    [
        ("hep-th/9901001v2", "arxiv:hep-th/9901001"),
        ("math.GT/0309136", "arxiv:math.gt/0309136"),
        ("isbn:978-3-16-148410-0", "isbn:978-3-16-148410-0"),
        ("ISBN:978-3-16-148410-0", "ISBN:978-3-16-148410-0"),  # not guessed at
        ("PMID:12345", "PMID:12345"),
        ("Some Local Key", "Some Local Key"),
        ("10.12/short", "10.12/short"),  # not a DOI registrant code
    ],
)
def test_other_identifiers(given, key):
    assert normalize_ext_id(given)[0] == key
    assert normalize_ext_id(key)[0] == key  # idempotent


def old_stub(conn, who, key: str) -> str:
    """A paper stub as an older version wrote it: the id kept as given."""
    rec = records.sign_and_append(
        conn,
        who,
        type="atom",
        address=fmt.new_address(who.person_id),
        payload={"kind": "paper", "text": "", "title": "Old", "ext": {"key": key}},
    )
    apply_record(conn, rec)
    conn.execute("UPDATE node SET ext_key = %s WHERE address = %s", (key, rec.address))
    return rec.address


def test_rebuild_rekeys_stubs_written_under_the_old_normalisation(conn, make_account):
    alice = make_account()
    old = old_stub(conn, alice, "10.1038/s41586-020-2649-2")
    assert node(conn, old)["ext_key"] == "10.1038/s41586-020-2649-2"
    rebuild(conn)
    assert node(conn, old)["ext_key"] == DOI
    # and a new put finds it instead of making a second stub
    assert addr(paper(conn, alice, "10.1038/s41586-020-2649-2", title="x")) == old
    assert papers(conn) == 1


def test_migration_rekeys_existing_stubs(conn, make_account):
    alice = make_account()
    keys = {
        "10.1038/S41586-020-2649-2": DOI,
        "2101.00001v2": "arxiv:2101.00001",
        "doi:10.1000/ABC": "doi:10.1000/abc",
        "arxiv:HEP-TH/9901001V2": "arxiv:hep-th/9901001",
        "arxiv:2101.00002": "arxiv:2101.00002",
        "isbn:978-3": "isbn:978-3",
    }
    addresses = {old: old_stub(conn, alice, old) for old in keys}
    conn.execute((migrations_dir() / "0201_paper_keys.sql").read_text())
    for old, new in keys.items():
        assert node(conn, addresses[old])["ext_key"] == new, old
