from __future__ import annotations

import pytest

from reticula_mcp import permissions
from reticula_mcp.errors import ReticulaError
from reticula_mcp.resolve import resolve_person, resolve_ref
from tests.helpers import make_atom


def readable(conn, viewer):
    return {
        r["node_id"]
        for r in conn.execute(
            permissions.readable_nodes_sql("%(viewer)s"), {"viewer": viewer.id}
        )
    }


def current(conn, viewer):
    return {
        r["node_id"]
        for r in conn.execute(
            permissions.current_nodes_sql("%(viewer)s"), {"viewer": viewer.id}
        )
    }


def test_stub_readable_nodes_is_own_non_tombstoned_atoms(conn, make_account):
    a, b = make_account(), make_account()
    n1 = make_atom(conn, a, "mine")
    n2 = make_atom(conn, a, "private", audience="private")
    gone = make_atom(conn, a, "deleted")
    conn.execute("UPDATE node SET tombstoned_at = now() WHERE id = %s", (gone["id"],))
    theirs = make_atom(conn, b, "theirs")
    assert readable(conn, a) == {n1["id"], n2["id"]}
    assert readable(conn, b) == {theirs["id"]}
    assert permissions.can_read(conn, a, n1["id"])
    assert not permissions.can_read(conn, a, theirs["id"])
    assert not permissions.can_read(conn, a, gone["id"])
    assert not permissions.can_read(conn, a, -1)


def test_current_nodes_hides_only_what_the_same_viewer_can_see_superseded(
    conn, make_account
):
    a, b = make_account(), make_account()
    old = make_atom(conn, a, "old")
    new = make_atom(conn, a, "new")
    conn.execute(
        "INSERT INTO supersedes (new_node_id, old_node_id) VALUES (%s, %s)",
        (new["id"], old["id"]),
    )
    assert readable(conn, a) == {old["id"], new["id"]}
    assert current(conn, a) == {new["id"]}
    # B supersedes A's note privately: A still sees A's own note as current...
    bnew = make_atom(conn, b, "b's replacement", audience="private")
    conn.execute(
        "INSERT INTO supersedes (new_node_id, old_node_id) VALUES (%s, %s)",
        (bnew["id"], new["id"]),
    )
    assert current(conn, a) == {new["id"]}
    # ...and under the stub rule B cannot read A's notes, so B's view is unaffected too
    assert current(conn, b) == {bnew["id"]}


def test_can_add_stub_is_hub_author_only(conn, make_account):
    a, b = make_account(), make_account()
    hub = make_atom(conn, a, "hub", kind="hub")
    assert permissions.can_add(conn, a, hub["id"])
    assert not permissions.can_add(conn, b, hub["id"])


def test_resolve_ref(conn, make_account):
    a, b = make_account(), make_account()
    n = make_atom(conn, a, "findme", title="Findable")
    theirs = make_atom(conn, b, "not yours")
    tail = n["address"][-8:]
    assert resolve_ref(conn, a, n["address"])["id"] == n["id"]
    assert resolve_ref(conn, a, tail)["id"] == n["id"]
    assert resolve_ref(conn, a, tail.upper())["id"] == n["id"]
    assert resolve_ref(conn, a, n["address"].split("/")[1])["id"] == n["id"]
    for ref in (theirs["address"], theirs["address"][-8:]):  # exists, but not readable
        with pytest.raises(ReticulaError) as e:
            resolve_ref(conn, a, ref)
        assert e.value.code == "NotFound"
    for ref in ("abc", "zzzzzzzz", "ret:nonsense", ""):
        with pytest.raises(ReticulaError) as e:
            resolve_ref(conn, a, ref)
        assert e.value.code == "BadInput"
    with pytest.raises(ReticulaError) as e:
        resolve_ref(conn, a, "ffffffffffff")
    assert e.value.code == "NotFound"


def test_resolve_ref_ambiguity_and_tombstones(conn, make_account):
    a = make_account()
    n1, n2 = make_atom(conn, a, "one"), make_atom(conn, a, "two", title="Second")
    # force a shared 6-char tail
    conn.execute(
        "UPDATE node SET address = left(address, length(address) - 6) || 'abcdef' WHERE id IN (%s,%s)",
        (n1["id"], n2["id"]),
    )
    with pytest.raises(ReticulaError) as e:
        resolve_ref(conn, a, "abcdef")
    assert e.value.code == "Conflict" and "ret:" in e.value.message and e.value.hint
    gone = make_atom(conn, a, "deleted")
    conn.execute("UPDATE node SET tombstoned_at = now() WHERE id = %s", (gone["id"],))
    with pytest.raises(ReticulaError) as e:
        resolve_ref(conn, a, gone["address"])
    assert e.value.code == "NotFound"
    assert (
        resolve_ref(conn, a, gone["address"], allow_tombstoned=True)["id"] == gone["id"]
    )


def test_resolve_person(conn, make_account):
    a = make_account("pers@example.com")
    assert resolve_person(conn, "@pers") == a
    assert resolve_person(conn, a.person_id) == a
    for ref, code in (
        ("@nobody", "NotFound"),
        ("p" + "a" * 26, "NotFound"),
        ("pers", "BadInput"),
    ):
        with pytest.raises(ReticulaError) as e:
            resolve_person(conn, ref)
        assert e.value.code == code
