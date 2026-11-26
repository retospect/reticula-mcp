"""delete in two steps, the same for every client: the first call deletes
nothing and names what would be deleted, with the call that confirms it; the
second, with a code bound to the account, the note and its version, deletes.
The first answer is the wording trialled (docs/trials.md, "Asking before a
delete"), word for word."""

from __future__ import annotations

import os
import re

import pytest

from reticula_mcp import accounts, keystore
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers as graph
from reticula_mcp.search import handlers as search
from tests.graph_support import confirm_ref, mk_hub, mk_note, node, share
from tests.search_support import use_hash_embedder


@pytest.fixture(autouse=True)
def _hash_embedder(monkeypatch):
    use_hash_embedder(monkeypatch)


def short(address: str) -> str:
    return address[-8:]


def test_the_first_call_deletes_nothing_and_says_how_to_confirm(conn, make_account):
    ada = make_account()
    hub = mk_hub(conn, ada, "Family")
    note = mk_note(
        conn, ada, "secret plans", title="Budget for Lena's present", hub=hub
    )
    out = graph.delete(conn, ada, ref=note)
    code = confirm_ref(out).rsplit(":yes-", 1)[1]
    assert out == (
        f"nothing deleted yet: this would delete {short(note)} \"Budget for Lena's"
        f' present" ({note}) (filed in hub "Family"). Ask the person; only after'
        f' they say yes: delete(ref="{short(note)}:yes-{code}")\n\nDo not make the'
        " second call before the person has said yes in this conversation."
    )
    assert node(conn, note)["tombstoned_at"] is None
    assert "secret plans" in search.get(conn, ada, ref=note)


def test_the_confirming_call_deletes_with_the_reply_of_before(conn, make_account):
    ada = make_account()
    note = mk_note(conn, ada, "x", title="Gone")
    out = graph.delete(conn, ada, ref=confirm_ref(graph.delete(conn, ada, ref=note)))
    assert out.startswith(f"deleted {short(note)}") and "content is erased" in out
    assert node(conn, note)["tombstoned_at"] is not None


@pytest.mark.parametrize("code", ["0000", "zz", "", "café", "12345", " 0000"])
def test_a_wrong_code_deletes_nothing(conn, make_account, code):
    ada = make_account()
    note = mk_note(conn, ada, "x", title="Kept")
    real = confirm_ref(graph.delete(conn, ada, ref=note)).rsplit(":yes-", 1)[1]
    if code.strip() == real:
        code = "ffff" if real != "ffff" else "fffe"
    with pytest.raises(ReticulaError) as exc:
        graph.delete(conn, ada, ref=f"{short(note)}:yes-{code}")
    assert exc.value.code == "BadInput"
    assert exc.value.message == "this confirmation does not match the note as it is now"
    assert exc.value.hint == f'delete(ref="{short(note)}") shows what would be deleted'
    assert node(conn, note)["tombstoned_at"] is None


def test_an_edit_in_between_makes_the_code_stale(conn, make_account):
    ada = make_account()
    note = mk_note(conn, ada, "first words", title="Draft")
    asked = graph.delete(conn, ada, ref=note)
    graph.edit(conn, ada, ref=note, text="other words")
    with pytest.raises(ReticulaError) as exc:
        graph.delete(conn, ada, ref=confirm_ref(asked))
    assert exc.value.code == "BadInput"
    assert node(conn, note)["tombstoned_at"] is None
    again = graph.delete(conn, ada, ref=note)  # asking again gives a new code
    assert confirm_ref(again) != confirm_ref(asked)
    graph.delete(conn, ada, ref=confirm_ref(again))
    assert node(conn, note)["tombstoned_at"] is not None


def test_only_the_author_is_asked_and_no_code_is_shown_to_others(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    hub = mk_hub(conn, ada, "Shared")
    share(conn, ada, hub, ben, "add")
    note = mk_note(conn, ada, "ada's note", hub=hub)
    code = confirm_ref(graph.delete(conn, ada, ref=note)).rsplit(":yes-", 1)[1]
    for ref in (note, f"{short(note)}:yes-{code}"):
        with pytest.raises(ReticulaError) as exc:
            graph.delete(conn, ben, ref=ref)
        assert exc.value.code == "Forbidden"
        assert code not in exc.value.render()
    assert node(conn, note)["tombstoned_at"] is None


def test_hubs_the_reader_may_not_read_are_not_named(conn, make_account):
    ada, ben = make_account(handle="ada"), make_account(handle="ben")
    open_hub = mk_hub(conn, ben, "Open")
    closed = mk_hub(conn, ben, "Ben's closed hub")
    share(conn, ben, open_hub, ada, "add")
    share(conn, ben, closed, ada, "add")
    note = mk_note(conn, ada, "ada's note", hub=open_hub)
    graph.link(conn, ada, ref=note, to=closed, rel="part-of")
    share(conn, ben, closed, ada, "none")  # ada loses the closed hub
    out = graph.delete(conn, ada, ref=note)
    assert '(filed in hub "Open")' in out and "closed" not in out


def test_a_note_in_no_hub_names_none(conn, make_account):
    ada = make_account()
    note = mk_note(conn, ada, "loose")
    out = graph.delete(conn, ada, ref=note)
    assert "filed in" not in out and ". Ask the person;" in out


# ------------------------------------------------------------------- the code


def test_the_code_holds_for_this_window_and_the_one_before():
    window = accounts.CONFIRMATION_WINDOW_SECONDS
    now = 1_000_000 * window + 10.0
    this = accounts.confirmation_code(7, 42, "sha256:v", int(now // window))
    before = accounts.confirmation_code(7, 42, "sha256:v", int(now // window) - 1)
    older = accounts.confirmation_code(7, 42, "sha256:v", int(now // window) - 2)
    assert re.fullmatch(r"[0-9a-f]{4}", this)
    assert accounts.confirmation_holds(7, 42, "sha256:v", this, now=now)
    assert accounts.confirmation_holds(7, 42, "sha256:v", before, now=now)
    assert not accounts.confirmation_holds(
        7, 42, "sha256:v", older, now=now
    ) or older in (
        this,
        before,
    )


def test_the_code_is_bound_to_account_note_and_version():
    window = accounts.confirmation_window()
    codes = {
        accounts.confirmation_code(7, 42, "sha256:v", window),
        accounts.confirmation_code(8, 42, "sha256:v", window),
        accounts.confirmation_code(7, 43, "sha256:v", window),
        accounts.confirmation_code(7, 42, "sha256:w", window),
    }
    # four hex digits: two of four may meet by chance, all four alike would not
    assert len(codes) >= 3


def test_the_code_needs_the_servers_secret(monkeypatch):
    """Another secret gives another code: nobody without the key file can
    compute one."""
    window = accounts.confirmation_window()
    samples = []
    for secret in (b"a" * 32, b"b" * 32):
        keystore.configure_secret(secret)
        try:
            samples.append(
                [accounts.confirmation_code(7, n, "sha256:v", window) for n in range(8)]
            )
            assert keystore.confirmation_key() not in (secret,)
        finally:
            keystore.configure(os.environ["RETICULA_KEY_FILE"])  # as conftest
    assert samples[0] != samples[1]
