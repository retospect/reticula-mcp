"""The directory of people: search(kind="person"), who is left out of it, and the
hint share gives for a handle that does not exist."""

from __future__ import annotations

import re

import pytest

from reticula_mcp import accounts, connection, settings
from reticula_mcp.content import handlers as content
from reticula_mcp.errors import ReticulaError
from reticula_mcp.graph import handlers
from reticula_mcp.search import handlers as search
from tests.graph_support import mk_hub

NEXT = (
    'next: share(hub=<hub>, person="@handle", mode="add" or "read")'
    " shares a hub with one of them"
)
LINE = re.compile(
    r"^- @(\S+)( \(you\))? · verified: (yes|no) · joined \d{4}-\d\d-\d\d$"
)


def find(conn, viewer, query="", **kwargs) -> str:
    return search.search(conn, viewer, query=query, kind="person", **kwargs)


def handles(text: str) -> list[str]:
    """The handles on the lines of a listing, in order."""
    return [m.group(1) for ln in text.splitlines() if (m := LINE.match(ln))]


def err(call, *args, **kwargs) -> ReticulaError:
    with pytest.raises(ReticulaError) as e:
        call(*args, **kwargs)
    return e.value


def unlist(conn, who, value: str = "no") -> None:
    """What the person does: edit(ref="settings", text="listed: no")."""
    with connection.during({"connection": "grant:people"}):
        handlers.edit(conn, who, ref="settings", text=f"listed: {value}")


def group(make_account, *names, **kwargs):
    return [make_account(handle=name, **kwargs) for name in names]


# --- the listing ---------------------------------------------------------------


def test_a_part_of_the_handle_finds_the_accounts_by_handle(conn, make_account):
    viewer, *_ = group(make_account, "zed", "frodo", "alfred", "bob")
    out = find(conn, viewer, "fr")
    assert out.splitlines()[0] == 'people matching "fr": 2'
    assert handles(out) == ["alfred", "frodo"]
    assert out.splitlines()[-1] == NEXT


def test_the_exact_output_of_a_listing(conn, make_account):
    viewer, *_ = group(make_account, "zed", "frodo", "alfred", "bob")
    today = conn.execute("SELECT now()::date AS d").fetchone()["d"]
    assert find(conn, viewer, "fr") == (
        'people matching "fr": 2\n'
        f"- @alfred · verified: no · joined {today}\n"
        f"- @frodo · verified: no · joined {today}\n" + NEXT
    )


def test_matching_ignores_case_and_a_leading_at(conn, make_account):
    viewer, *_ = group(make_account, "zed", "frodo", "alfred")
    for query in ("FR", "Fr", "@fr", "  @fr  "):
        assert handles(find(conn, viewer, query)) == ["alfred", "frodo"]
    assert handles(find(conn, viewer, "@FRO")) == ["frodo"]
    assert find(conn, viewer, "@frodo").splitlines()[0] == 'people matching "frodo": 1'


def test_an_empty_query_lists_everyone_who_is_listed(conn, make_account):
    group(make_account, "zed", "frodo", "alfred")
    viewer = make_account(handle="viewer")
    total = conn.execute(
        "SELECT count(*) AS n FROM account"
        f" WHERE NOT ({settings.unlisted_sql('account.id')})"
        " AND status IN ('active', 'provisional')"
    ).fetchone()["n"]
    out = find(conn, viewer)
    assert out.splitlines()[0] == f"people on this instance: {total} listed"
    assert {"zed", "frodo", "alfred", "viewer"} <= set(handles(out))
    assert find(conn, viewer, "@").splitlines()[0] == out.splitlines()[0]


def test_the_list_is_ordered_by_handle(conn, make_account):
    viewer, *_ = group(make_account, "p-c", "p-a", "p-b")
    assert handles(find(conn, viewer, "p-")) == ["p-a", "p-b", "p-c"]


def test_nobody_matches(conn, make_account):
    viewer = make_account(handle="viewer")
    assert find(conn, viewer, "qqqq") == (
        'no listed person matches "qqqq". Someone who is not listed can still be'
        " shared with by their exact handle."
    )


def test_a_viewer_is_marked_with_you_only_on_their_own_line(conn, make_account):
    viewer, other = group(make_account, "fr-me", "fr-other")
    out = find(conn, viewer, "fr-")
    assert [ln.split(" ·")[0] for ln in out.splitlines()[1:3]] == [
        "- @fr-me (you)",
        "- @fr-other",
    ]
    again = find(conn, other, "fr-")
    assert "- @fr-other (you)" in again and "- @fr-me (you)" not in again


def test_a_viewer_who_is_not_listed_does_not_appear_and_has_no_mark(conn, make_account):
    viewer, other = group(make_account, "hid-me", "hid-other")
    unlist(conn, viewer)
    out = find(conn, viewer, "hid-")
    assert handles(out) == ["hid-other"]
    assert "(you)" not in out


def test_verified_accounts_say_yes(conn, make_account):
    viewer, other = group(make_account, "ver-a", "ver-b")
    accounts.set_verified(conn, other)
    out = find(conn, viewer, "ver-")
    assert "- @ver-a (you) · verified: no" in out
    assert "- @ver-b · verified: yes" in out


# --- who is never listed -------------------------------------------------------


def test_an_unlisted_account_is_not_shown_but_can_still_be_shared_with(
    conn, make_account
):
    owner, quiet = group(make_account, "own-a", "own-quiet")
    unlist(conn, quiet)
    assert handles(find(conn, owner, "own-")) == ["own-a"]
    assert find(conn, owner, "own-").splitlines()[0] == 'people matching "own-": 1'
    hub = mk_hub(conn, owner, "Team")
    out = handlers.share(conn, owner, hub=hub, person="@own-quiet", mode="read")
    assert "@own-quiet" in out


def test_a_disabled_account_is_never_listed(conn, make_account):
    viewer, gone = group(make_account, "dis-a", "dis-gone")
    conn.execute("UPDATE account SET status = 'disabled' WHERE id = %s", (gone.id,))
    assert handles(find(conn, viewer, "dis-")) == ["dis-a"]


def test_a_provisional_account_is_listed(conn, make_account):
    viewer, prov = group(make_account, "prov-a", "prov-new")
    conn.execute("UPDATE account SET status = 'provisional' WHERE id = %s", (prov.id,))
    assert handles(find(conn, viewer, "prov-")) == ["prov-a", "prov-new"]


def test_the_system_account_is_never_listed(conn, make_account):
    viewer = make_account(handle="viewer")
    system = accounts.ensure_system_account(conn)
    assert system.handle not in handles(find(conn, viewer))
    assert handles(find(conn, viewer, system.handle)) == []
    unlist(conn, system, "yes")  # even a system account that says yes
    assert system.handle not in handles(find(conn, viewer))


def test_an_email_address_or_person_id_is_never_printed(conn, make_account):
    viewer = make_account(email="secret.viewer@example.com", handle="mail-a")
    other = make_account(email="hidden.other@example.com", handle="mail-b")
    for query in ("", "mail-", "@mail-b"):
        out = find(conn, viewer, query)
        assert "example.com" not in out and "secret" not in out, out
        assert "hidden" not in out, out
        assert viewer.person_id not in out and other.person_id not in out, out
    # the e-mail address is not searched either: a part of it finds nobody
    for query in ("secret", "example.com", "hidden", viewer.person_id):
        out = find(conn, viewer, query)
        assert out.startswith("no listed person matches"), out
        assert "mail-" not in out
    # the line carries the handle, the verified flag and the day, nothing else
    for line in find(conn, viewer, "mail-").splitlines()[1:3]:
        assert LINE.match(line), line


# --- wildcards and limits ------------------------------------------------------


def test_percent_matches_only_a_handle_with_a_percent_in_it(conn, make_account):
    # a handle cannot hold % (a-z 0-9 _ -), so a literal % matches nobody
    viewer, *_ = group(make_account, "wild_card", "wildcard", "wild-card")
    assert find(conn, viewer, "%").startswith("no listed person matches")
    assert find(conn, viewer, "wild%").startswith("no listed person matches")
    assert find(conn, viewer, "\\").startswith("no listed person matches")


def test_underscore_matches_only_a_handle_with_an_underscore(conn, make_account):
    viewer, *_ = group(make_account, "und_one", "undtwo", "und-three")
    out = find(conn, viewer, "und_")
    assert handles(out) == ["und_one"]
    assert out.splitlines()[0] == 'people matching "und_": 1'
    with_underscore = handles(find(conn, viewer, "_"))
    assert "und_one" in with_underscore and "undtwo" not in with_underscore
    assert all("_" in h for h in with_underscore)


def test_the_limit_cuts_the_list_and_says_how_many_more(conn, make_account):
    viewer, *_ = group(make_account, "lim-a", "lim-b", "lim-c", "lim-d", "lim-e")
    out = find(conn, viewer, "lim-", limit=2)
    assert out.splitlines()[0] == 'people matching "lim-": 5'
    assert handles(out) == ["lim-a", "lim-b"]
    assert out.splitlines()[3] == (
        "3 more; a longer part of the handle narrows the list"
    )
    assert out.splitlines()[-1] == NEXT
    whole = find(conn, viewer, "lim-", limit=5)
    assert "more;" not in whole and len(handles(whole)) == 5


def test_the_limit_is_clamped_like_other_searches(conn, make_account):
    viewer = make_account(handle="clamp")
    assert handles(find(conn, viewer, "clamp", limit=0)) == ["clamp"]
    assert handles(find(conn, viewer, "clamp", limit=-5)) == ["clamp"]


def test_a_long_query_is_refused_as_in_every_search(conn, make_account):
    viewer = make_account()
    assert err(find, conn, viewer, "x" * 1001).code == "BadInput"


# --- kind="person" takes only a query and a limit ------------------------------


def test_person_with_hub_since_or_include_superseded_is_bad_input(conn, make_account):
    viewer = make_account()
    hub = mk_hub(conn, viewer, "Team")
    for extra in ({"hub": hub}, {"since": "7d"}, {"include_superseded": True}):
        e = err(find, conn, viewer, "x", **extra)
        assert e.code == "BadInput"
        assert e.message == (
            "kind='person' lists accounts; it takes no hub, since or include_superseded"
        )


def test_the_kind_is_read_like_the_other_kinds(conn, make_account):
    viewer = make_account(handle="kindy")
    out = search.search(conn, viewer, query="kindy", kind=" Person ")
    assert handles(out) == ["kindy"]


def test_other_kinds_are_not_affected(conn, make_account):
    viewer = make_account()
    assert err(search.search, conn, viewer, query="x", kind="people").code == (
        "BadInput"
    )


# --- the setting (reticula_mcp.settings) ---------------------------------------


def test_a_new_account_is_listed_until_it_says_no(conn, make_account):
    z = make_account(handle="fresh-one")
    assert settings.listed(conn, z)
    assert handles(find(conn, z, "fresh-one")) == ["fresh-one"]


def test_listed_yes_puts_an_account_back(conn, make_account):
    z, other = group(make_account, "back-z", "back-o")
    unlist(conn, z)
    assert handles(find(conn, other, "back-")) == ["back-o"]
    unlist(conn, z, "yes")
    assert handles(find(conn, other, "back-")) == ["back-o", "back-z"]


def test_an_unlisted_account_is_left_out_of_the_count_and_the_limit(conn, make_account):
    accounts_ = group(make_account, "cnt-a", "cnt-b", "cnt-c")
    unlist(conn, accounts_[0])
    out = find(conn, accounts_[1], "cnt-", limit=1)
    assert out.splitlines()[0] == 'people matching "cnt-": 2'
    assert handles(out) == ["cnt-b"]
    assert "1 more;" in out


def test_profile_lists_the_setting_with_its_wording(conn, make_account):
    z = make_account()
    with connection.during({"connection": "grant:people"}):
        out = content.profile(conn, z)
    assert (
        '- listed: yes (this account): search(kind="person") shows this'
        " account's handle to other people on this instance"
    ) in out.splitlines()


def test_the_same_handle_alone_keeps_its_answer(conn, make_account):
    z = make_account(handle="same-h")
    assert content.profile(conn, z, handle="same-h") == (
        "Handle is already @same-h. Nothing changed."
    )


# --- share ---------------------------------------------------------------------


def test_share_to_an_unknown_handle_hints_at_the_search(conn, make_account):
    owner = make_account()
    hub = mk_hub(conn, owner, "Team")
    e = err(handlers.share, conn, owner, hub=hub, person="@nobody-here", mode="read")
    assert e.code == "NotFound"
    assert e.message == "no such person '@nobody-here'"
    assert e.hint == (
        'search(query="nobody-here", kind="person") lists people whose handle'
        " contains it"
    )


def test_share_to_a_disabled_handle_gets_the_same_hint(conn, make_account):
    owner, gone = group(make_account, "dg-owner", "dg-gone")
    conn.execute("UPDATE account SET status = 'disabled' WHERE id = %s", (gone.id,))
    hub = mk_hub(conn, owner, "Team")
    e = err(handlers.share, conn, owner, hub=hub, person="@dg-gone")
    assert e.code == "NotFound" and 'kind="person"' in (e.hint or "")


def test_share_to_an_unknown_person_id_keeps_its_plain_answer(conn, make_account):
    owner = make_account()
    hub = mk_hub(conn, owner, "Team")
    e = err(handlers.share, conn, owner, hub=hub, person="p" + "a" * 26)
    assert e.code == "NotFound" and e.hint is None


def test_share_with_a_malformed_person_keeps_its_bad_input(conn, make_account):
    owner = make_account()
    hub = mk_hub(conn, owner, "Team")
    e = err(handlers.share, conn, owner, hub=hub, person="nobody")
    assert e.code == "BadInput"


def test_a_viewer_who_may_not_grant_is_refused_before_the_lookup(conn, make_account):
    owner, outsider, reader = group(make_account, "pc-o", "pc-x", "pc-r")
    hub = mk_hub(conn, owner, "Team")
    handlers.share(conn, owner, hub=hub, person="@pc-r", mode="read")
    for viewer in (outsider, reader):
        for mode in ("none", "read", "add"):
            known = err(
                handlers.share, conn, viewer, hub=hub, person="@pc-o", mode=mode
            )
            unknown = err(
                handlers.share, conn, viewer, hub=hub, person="@pc-ghost", mode=mode
            )
            assert (known.code, known.message, known.hint) == (
                unknown.code,
                unknown.message,
                unknown.hint,
            )
            assert unknown.hint is None or "kind=" not in unknown.hint
    # a reader who may not grant gets Forbidden, not the lookup hint
    assert (
        err(handlers.share, conn, reader, hub=hub, person="@pc-ghost", mode="read").code
        == "Forbidden"
    )
