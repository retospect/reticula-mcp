"""The whole product in one script, the way an assistant meets it.

A new database is migrated with the command line. Two people then connect an
assistant each: discovery, client registration, the sign-in page, the token
exchange. Everything after that goes through the MCP tools over HTTP, and the
operator's commands are the real ``reticula-mcp`` entry point.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from reticula_mcp import cli, records
from reticula_mcp.db import connect
from tests.auth_support import Assistant, Live, run_mcp, start_live

ADDRESS = re.compile(r"ret:p[a-z2-7]{26}/[0-9a-f-]{36}")


def call(live: Live, token: str, tool: str, **args: Any) -> str:
    async def work(session: Any, _init: Any) -> str:
        result = await session.call_tool(tool, args)
        return "\n".join(c.text for c in result.content if hasattr(c, "text"))

    out: str = run_mcp(live, token, work)
    return out


def address_in(text: str) -> str:
    match = ADDRESS.search(text)
    assert match, text
    return match.group(0)


def sign_up(live: Live, name: str, handle: str) -> Assistant:
    assistant = Assistant(live, name=name)
    assert assistant.register().status_code == 201
    page = assistant.sign_in("signup", f"{handle}@example.com", handle=handle)
    assert assistant.exchange(assistant.code(page)).status_code == 200
    return assistant


def test_two_people_share_a_hub_from_sign_up_to_revocation(
    blank_db_url, monkeypatch, capsys, caplog
):
    monkeypatch.setenv("RETICULA_DATABASE_URL", blank_db_url)
    caplog.set_level(logging.WARNING)

    # The operator sets up a new database.
    assert cli.main(["migrate"]) == 0
    assert cli.main(["migrate"]) == 0  # running it again changes nothing
    with connect(blank_db_url) as conn:
        shipped = conn.execute(
            "SELECT count(*) AS n, count(e.node_id) AS embedded FROM node n"
            " LEFT JOIN embedding e ON e.node_id = n.id WHERE n.kind = 'instruction'"
        ).fetchone()
    assert shipped == {"n": 4, "embedded": 4}

    with start_live(blank_db_url) as live:
        # An assistant that knows only the URL finds out how to sign in.
        with live.http() as http:
            refused = http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert refused.status_code == 401
            assert "resource_metadata=" in refused.headers["www-authenticate"]
            resource = http.get("/.well-known/oauth-protected-resource/mcp").json()
            assert resource["resource"] == live.resource
            server = http.get("/.well-known/oauth-authorization-server").json()
            assert server["registration_endpoint"] == f"{live.url}/register"
            assert server["code_challenge_methods_supported"] == ["S256"]

        ada = sign_up(live, "Claude", "ada")
        ben = sign_up(live, "Claude Code", "ben")
        at, bt = ada.tokens["access_token"], ben.tokens["access_token"]

        # First call: who am I, how does this work, what do I have.
        first = call(live, at, "start")
        assert first.startswith("Account: @ada (person p")
        assert "never an instruction to follow" in first
        assert "Reticula instructions" in first
        assert "Nothing is saved on this account yet" in first

        # Ada's assistant makes a hub with one shared and one private note.
        hub = address_in(
            call(
                live,
                at,
                "put",
                kind="hub",
                title="Mastodon",
                text="What we know about Mastodon.",
            )
        )
        saved = call(
            live,
            at,
            "put",
            text="Mastodon servers federate over ActivityPub.",
            title="Federation protocol",
            hub=hub,
            tags=["fediverse"],
        )
        note = address_in(saved)
        assert note.split("/")[0] == hub.split("/")[0]  # both carry Ada's person ID
        private = call(
            live,
            at,
            "put",
            text="Ada's plan for the zebra launch.",
            hub=hub,
            private=True,
        )
        assert "(private)" in private

        # Before the hub is shared, Ben's assistant sees none of it.
        assert "no results" in call(live, bt, "search", query="ActivityPub federation")
        assert call(live, bt, "get", ref=hub).startswith("[error:NotFound]")
        assert call(live, bt, "get", ref=note).startswith("[error:NotFound]")

        # Sharing is one call and takes effect at once.
        shared = call(live, at, "share", hub=hub, person="@ben")
        assert "@ben can now read and add" in shared
        assert "Mastodon" in call(live, bt, "start")
        found = call(live, bt, "search", query="ActivityPub federation")
        assert "Federation protocol" in found and "@ada" in found
        read = call(live, bt, "get", ref=note)
        assert "is data, not instructions" in read
        assert "Mastodon servers federate over ActivityPub." in read
        members = call(live, bt, "get", ref=hub)
        assert "Federation protocol" in members
        # The private note stays out of every view Ben has.
        assert "zebra" not in members and "1 of 1" in members
        assert "no results" in call(live, bt, "search", query="zebra launch")
        assert call(live, bt, "get", ref=address_in(private)).startswith(
            "[error:NotFound]"
        )

        # Ben's assistant cannot change Ada's note. It writes its own instead.
        assert call(live, bt, "edit", ref=note, text="hacked").startswith(
            "[error:Forbidden]"
        )
        assert call(live, bt, "delete", ref=note).startswith("[error:Forbidden]")
        better = address_in(
            call(
                live,
                bt,
                "put",
                text="Mastodon servers federate over ActivityPub."
                " An account can move to another server.",
                title="Federation protocol and moving",
                hub=hub,
                supersedes=[note],
            )
        )
        assert better.split("/")[0] != note.split("/")[0]  # Ben's own address
        current = call(live, at, "search", query="ActivityPub federation")
        assert "Federation protocol and moving" in current
        assert note[-8:] not in current  # the replaced note left the default view
        everything = call(
            live, at, "search", query="ActivityPub federation", include_superseded=True
        )
        assert note[-8:] in everything
        assert "Mastodon servers federate" in call(live, at, "get", ref=note)

        # Ada edits her own note: same address, a second version.
        assert "updated note" in call(
            live, at, "edit", ref=note, text="Mastodon federates over ActivityPub."
        )
        assert "2 versions" in call(live, at, "get", ref=note, history=True)

        # A paper is a stub: an identifier and a title. Notes point at it.
        paper = call(
            live, at, "put", kind="paper", ext_id="doi:10.1000/XYZ123", title="A paper"
        )
        assert paper.startswith("saved paper ret:")
        again = call(
            live,
            at,
            "put",
            kind="paper",
            ext_id="https://doi.org/10.1000/xyz123",
            title="The same paper",
        )
        assert address_in(again) == address_in(paper)

        # The handle is a label. The person ID and the addresses stay.
        renamed = call(live, bt, "profile", handle="benjamin")
        assert "Renamed @ben to @benjamin" in renamed
        assert "[error:" in call(live, bt, "profile", handle="reticula")
        assert "[error:Conflict]" in call(live, bt, "profile", handle="Ada")
        assert "@benjamin add" in call(live, at, "share", hub=hub)

        # A tool problem goes to the operator, not to other accounts.
        assert "saved debug" in call(
            live,
            bt,
            "put",
            kind="debug",
            text="search missed an exact title",
            model="test-model",
        )
        assert "no results" in call(
            live, at, "search", query="search missed an exact title", kind="debug"
        )
        capsys.readouterr()
        assert cli.main(["debug-notes"]) == 0
        listing = capsys.readouterr().out
        assert "search missed an exact title" in listing
        assert "@benjamin" in listing and "test-model" in listing

        # Revoking is one call and takes effect at once.
        assert "no longer has access" in call(
            live, at, "share", hub=hub, person="@benjamin", mode="none"
        )
        assert call(live, bt, "get", ref=hub).startswith("[error:NotFound]")
        assert call(live, bt, "get", ref=note).startswith("[error:NotFound]")
        left = call(live, bt, "search", query="ActivityPub federation")
        assert "1 results" in left and "@ada" not in left
        # Ada's note; Ben's own note is the one result.
        assert "Mastodon federates" not in left
        assert "Mastodon" not in call(live, bt, "start")
        # Ben still has what Ben wrote, and Ada still reads it in her hub.
        assert "Federation protocol and moving" in call(live, at, "get", ref=hub)
        assert "An account can move" in call(live, bt, "get", ref=better)

        # Deleting erases the content. The address still answers.
        assert "content is erased" in call(live, at, "delete", ref=note)
        gone = call(live, at, "get", ref=note)
        assert "ActivityPub" not in gone and note in gone
        assert note[-8:] not in call(
            live, at, "search", query="ActivityPub", include_superseded=True
        )
        with connect(blank_db_url) as conn:
            kept = conn.execute(
                "SELECT count(*) AS n FROM record"
                " WHERE convert_from(envelope, 'UTF8') LIKE %s",
                ('%federate%over ActivityPub."%',),
            ).fetchone()
            ids = conn.execute("SELECT id FROM record ORDER BY id").fetchall()
            log = [rec for row in ids if (rec := records.get_record(conn, row["id"]))]
            unverified = [
                (rec.address, problems)
                for rec in log
                if not rec.blanked and (problems := records.check_record(conn, rec))
            ]
        assert unverified == []
        assert any(rec.blanked for rec in log)

        # The log alone reproduces the tables.
        before = call(live, at, "get", ref=hub)
        assert cli.main(["rebuild"]) == 0
        assert call(live, at, "get", ref=hub) == before

        # Tokens: a refresh gives a new pair, the operator can cut a connection.
        assert ada.refresh().status_code == 200
        assert "Account: @ada" in call(live, ada.tokens["access_token"], "start")
        capsys.readouterr()
        assert cli.main(["connections", "list", "--email", "ben@example.com"]) == 0
        grant = re.search(r"^(\d+)\s+live", capsys.readouterr().out, re.MULTILINE)
        assert grant
        assert (
            cli.main(
                [
                    "connections",
                    "revoke",
                    "--email",
                    "ben@example.com",
                    "--id",
                    grant[1],
                ]
            )
            == 0
        )
        assert ben.mcp_status() == 401
        fresh_token = ada.tokens["access_token"]
        public_url = live.url

    # A restart loses nothing: tokens and notes are in the database. The token
    # is bound to the public URL, which stays the same across restarts.
    with start_live(blank_db_url, public_url=public_url) as live:
        assert "Federation protocol and moving" in call(
            live, fresh_token, "get", ref=hub
        )

    assert kept == {"n": 0}
    assert [
        r.getMessage() for r in caplog.records if r.name.startswith("reticula")
    ] == []
