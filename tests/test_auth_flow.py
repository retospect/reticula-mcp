"""Sign-in lane acceptance scenarios, driving the real ASGI app over HTTP and MCP.

One scenario test per acceptance line (numbered as in the lane brief).
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from reticula_mcp import accounts
from reticula_mcp.auth import config
from reticula_mcp.db import connect
from tests.auth_support import (
    CLAUDE_REDIRECT,
    LOOPBACK_REDIRECT,
    PASSWORD,
    Assistant,
    Live,
    query_of,
    run_mcp,
    start_live,
)


@pytest.fixture
def live_auth(fresh_db_url):
    with start_live(fresh_db_url) as live:
        yield live


def make_account(live: Live, email: str, password: str = PASSWORD, handle=None):
    with connect(live.db_url) as conn:
        account = accounts.create_account(conn, email, password, handle)
        conn.commit()
    return account


# 1 -----------------------------------------------------------------------------


def test_1_new_user_goes_from_connector_url_to_a_first_tool_call(live_auth):
    live = live_auth
    http = live.http()

    # The assistant is given only the connector URL and POSTs to it: 401 + challenge.
    r = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert 'resource_metadata="' in challenge
    prm_url = challenge.split('resource_metadata="')[1].split('"')[0]

    # Discovery: protected resource -> authorization server -> endpoints.
    prm = httpx.get(prm_url).json()
    assert prm["resource"] == live.resource
    assert prm["authorization_servers"] == [live.url]
    meta = httpx.get(
        f"{prm['authorization_servers'][0]}/.well-known/oauth-authorization-server"
    ).json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["registration_endpoint"] == f"{live.url}/register"

    # Register, authorize, ONE page (create account), code, token.
    a = Assistant(live, name="Claude", redirect_uri=CLAUDE_REDIRECT)
    assert a.register().status_code == 201
    auth = a.authorize()
    assert auth.status_code == 302
    assert auth.headers["location"].startswith(f"{live.url}/signin?req=")
    page = a.open_page(auth.headers["location"], "signup")
    assert page.status_code == 200
    assert "Claude" in page.text and "Create account" in page.text
    done = a.submit(page, mode="signup", email="new@example.com", handle="newbie")
    code = a.code(done)
    token = a.exchange(code)
    assert token.status_code == 200
    body = token.json()
    assert body["token_type"].lower() == "bearer" and body["expires_in"] == 3600
    assert body["refresh_token"] and body["access_token"]

    # tools/list and one tool call over MCP with the issued token.
    async def work(session, init):
        tools = (await session.list_tools()).tools
        result = await session.call_tool("start", {})
        return [t.name for t in tools], result.content[0].text

    names, text = run_mcp(live, body["access_token"], work)
    assert "start" in names and "put" in names
    assert not text.startswith("[error:Forbidden]"), text

    # The access token names the new account.
    row = live.sql(
        "SELECT a.handle FROM oauth_token t JOIN account a ON a.id = t.account_id"
        " WHERE t.kind = 'access' AND t.client_id = %s",
        (a.client_id,),
    )
    assert row == [{"handle": "newbie"}]


# 2 -----------------------------------------------------------------------------


def test_2_returning_user_signs_in_wrong_password_refused_and_throttled(live_auth):
    live = live_auth
    make_account(live, "back@example.com", handle="backer")
    a = Assistant(live)
    a.register()

    # wrong password: refused with the generic text, no redirect, no code
    bad = a.sign_in("signin", "back@example.com", "not the password at all")
    assert bad.status_code == 401
    assert "do not match an account" in bad.text
    assert "location" not in bad.headers
    # an unknown e-mail is indistinguishable from a wrong password
    unknown = a.sign_in("signin", "nobody@example.com")
    assert unknown.status_code == 401
    assert "do not match an account" in unknown.text
    assert live.sql("SELECT count(*) AS n FROM oauth_code")[0]["n"] == 0

    # the right password on the same page works
    ok = a.sign_in("signin", "back@example.com")
    assert a.exchange(a.code(ok)).status_code == 200

    # failures are counted per e-mail: after the limit even the right password waits
    for _ in range(config.FAILURES_PER_EMAIL):
        r = a.sign_in("signin", "target@example.com", "wrong password here")
    make_account(live, "target@example.com")
    locked = a.sign_in("signin", "target@example.com")
    assert locked.status_code == 429
    assert "Too many attempts" in locked.text
    assert "location" not in locked.headers


def test_2_failures_are_throttled_per_address(live_auth):
    live = live_auth
    a = Assistant(live)
    a.register()
    for n in range(config.FAILURES_PER_IP):
        r = a.sign_in("signin", f"user{n}@example.com", "wrong password here")
        assert r.status_code == 401
    r = a.sign_in("signin", "fresh@example.com", "wrong password here")
    assert r.status_code == 429


# 3 -----------------------------------------------------------------------------


def test_3_each_assistant_has_its_own_token_revoking_one_leaves_the_other(live_auth):
    live = live_auth
    account = make_account(live, "two@example.com")
    first = Assistant(live, name="Claude", redirect_uri=CLAUDE_REDIRECT)
    second = Assistant(live, name="Inspector", redirect_uri=LOOPBACK_REDIRECT)
    t1 = first.connect("signin", "two@example.com")
    t2 = second.connect("signin", "two@example.com")
    assert t1["access_token"] != t2["access_token"]
    assert first.mcp_status() == 200 and second.mcp_status() == 200

    rows = live.sql(
        "SELECT label, client_id FROM oauth_grant WHERE account_id = %s ORDER BY id",
        (account.id,),
    )
    assert [r["label"] for r in rows] == ["Claude", "Inspector"]

    # revoke the first through the endpoint (public client: client_id, no secret)
    r = first.api.post(
        "/revoke", data={"token": t1["refresh_token"], "client_id": first.client_id}
    )
    assert r.status_code == 200
    assert first.mcp_status(t1["access_token"]) == 401
    assert first.refresh(t1["refresh_token"]).status_code == 400
    assert second.mcp_status() == 200
    assert second.refresh().status_code == 200


def test_3_replaying_a_consumed_refresh_token_kills_its_grant(live_auth):
    live = live_auth
    make_account(live, "rot@example.com")
    a = Assistant(live)
    old = a.connect("signin", "rot@example.com")
    other = Assistant(live, name="Other", redirect_uri=LOOPBACK_REDIRECT)
    other.connect("signin", "rot@example.com")

    rotated = a.refresh(old["refresh_token"])
    assert rotated.status_code == 200
    new = rotated.json()
    assert new["refresh_token"] != old["refresh_token"]
    assert a.mcp_status(new["access_token"]) == 200

    # someone presents the OLD refresh token again: refused, and the grant dies
    replay = a.refresh(old["refresh_token"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert a.mcp_status(new["access_token"]) == 401
    assert a.refresh(new["refresh_token"]).status_code == 400
    assert a.mcp_status(old["access_token"]) == 401
    # the other assistant is untouched
    assert other.mcp_status() == 200


def test_3_cli_lists_and_revokes_connections(live_auth, capsys):
    from reticula_mcp.cli import main

    live = live_auth
    make_account(live, "cli@example.com")
    a = Assistant(live, name="Claude Desktop")
    a.connect("signin", "cli@example.com")
    env = {"RETICULA_DATABASE_URL": live.db_url}
    import os

    os.environ.update(env)
    try:
        assert main(["connections", "list", "--email", "cli@example.com"]) == 0
        out = capsys.readouterr().out
        assert "Claude Desktop" in out and "live" in out
        grant_id = live.sql("SELECT id FROM oauth_grant")[0]["id"]
        assert (
            main(
                [
                    "connections",
                    "revoke",
                    "--email",
                    "cli@example.com",
                    "--id",
                    str(grant_id),
                ]
            )
            == 0
        )
        assert "revoked connection" in capsys.readouterr().out
        assert main(["connections", "list", "--email", "cli@example.com"]) == 0
        assert "revoked" in capsys.readouterr().out
        assert (
            main(
                ["connections", "revoke", "--email", "cli@example.com", "--id", "9999"]
            )
            == 1
        )
    finally:
        os.environ.pop("RETICULA_DATABASE_URL", None)
    assert a.mcp_status() == 401


def test_3_operator_minted_tokens_still_work_next_to_oauth_tokens(live_auth):
    live = live_auth
    account = make_account(live, "op@example.com")
    with connect(live.db_url) as conn:
        token = accounts.mint_token(conn, account, "laptop")
        conn.commit()
    a = Assistant(live)
    assert a.mcp_status(token) == 200
    a.connect("signin", "op@example.com")
    assert a.mcp_status() == 200 and a.mcp_status(token) == 200


# 4 -----------------------------------------------------------------------------


def test_4_signups_are_rate_limited_per_address(fresh_db_url):
    with start_live(fresh_db_url, signup_rate="3/hour") as live:
        a = Assistant(live)
        a.register()
        for n in range(3):
            r = a.sign_in("signup", f"limit{n}@example.com")
            assert r.status_code == 303, r.text
        refused = a.sign_in("signup", "limit3@example.com")
        assert refused.status_code == 429
        assert "Too many accounts" in refused.text
        assert (
            live.sql(
                "SELECT count(*) AS n FROM account WHERE email = 'limit3@example.com'"
            )[0]["n"]
            == 0
        )
        assert live.sql("SELECT count(*) AS n FROM signup_attempt")[0]["n"] == 3
        # another address has its own allowance (trusted proxy hands it over)
    with start_live(
        fresh_db_url, signup_rate="1/hour", trusted_proxies=("127.0.0.1",)
    ) as live:
        a = Assistant(live)
        a.register()
        a.browser.headers["X-Forwarded-For"] = "203.0.113.5"
        assert a.sign_in("signup", "xff1@example.com").status_code == 303
        assert a.sign_in("signup", "xff2@example.com").status_code == 429
        a.browser.headers["X-Forwarded-For"] = "203.0.113.6"
        assert a.sign_in("signup", "xff3@example.com").status_code == 303


def test_4_forwarded_for_is_ignored_from_untrusted_peers(fresh_db_url):
    with start_live(fresh_db_url, signup_rate="1/hour") as live:
        a = Assistant(live)
        a.register()
        a.browser.headers["X-Forwarded-For"] = "203.0.113.5"
        assert a.sign_in("signup", "u1@example.com").status_code == 303
        a.browser.headers["X-Forwarded-For"] = "203.0.113.6"
        assert a.sign_in("signup", "u2@example.com").status_code == 429


def test_4_closed_signup_refuses_new_accounts_but_not_sign_in(fresh_db_url):
    with start_live(fresh_db_url, signup="closed") as live:
        make_account(live, "old@example.com")
        a = Assistant(live)
        a.register()
        page_resp = a.authorize()
        page = a.open_page(page_resp.headers["location"], "signup")
        assert "not open" in page.text and 'type="password"' not in page.text
        # a hostile client can still POST the sign-up mode with a valid token
        signin_page = a.open_page(page_resp.headers["location"], "signin")
        refused = a.submit(signin_page, mode="signup", email="new@example.com")
        assert refused.status_code == 403
        assert "not open" in refused.text
        assert (
            live.sql(
                "SELECT count(*) AS n FROM account WHERE email = 'new@example.com'"
            )[0]["n"]
            == 0
        )
        assert (
            a.exchange(a.code(a.sign_in("signin", "old@example.com"))).status_code
            == 200
        )


# 5 -----------------------------------------------------------------------------


def _ready(live: Live, email: str = "ref@example.com", **kw) -> Assistant:
    make_account(live, email)
    a = Assistant(live, **kw)
    assert a.register().status_code == 201
    return a


def _no_token(live: Live) -> None:
    assert (
        live.sql("SELECT count(*) AS n FROM oauth_token WHERE client_id IS NOT NULL")[
            0
        ]["n"]
        == 0
    )


def test_5_pkce_failures_are_refused_without_a_token(live_auth):
    live = live_auth
    a = _ready(live)
    # wrong verifier
    code = a.code(a.sign_in("signin", "ref@example.com"))
    r = a.exchange(code, code_verifier="x" * 50)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    _no_token(live)
    # no challenge at all / plain method / malformed challenge never reach the page
    for overrides in (
        {"code_challenge": None},
        {"code_challenge_method": "plain"},
        {"code_challenge": "short"},
        {"code_challenge": "!" * 43},
    ):
        r = a.authorize(**overrides)
        assert r.status_code in (302, 400)
        if r.status_code == 302:
            assert "/signin" not in r.headers["location"], overrides
            assert query_of(r.headers["location"])["error"] == "invalid_request"
    assert (
        live.sql("SELECT count(*) AS n FROM oauth_pending")[0]["n"] == 0
    )  # none parked


def test_5_unknown_client_is_refused(live_auth):
    live = live_auth
    a = _ready(live)
    a.client_id = "c_does-not-exist"
    r = a.authorize()
    assert r.status_code == 400 and "location" not in r.headers
    r = a.exchange("rtc_" + "A" * 40)
    assert r.status_code == 401 or r.status_code == 400
    _no_token(live)


def test_5_unregistered_redirect_uri_is_refused_and_never_redirected_to(live_auth):
    live = live_auth
    a = _ready(live)
    r = a.authorize(redirect_uri="https://evil.example/cb")
    assert r.status_code == 400
    assert "location" not in r.headers
    assert live.sql("SELECT count(*) AS n FROM oauth_pending")[0]["n"] == 0
    # an almost-equal URI is a different URI
    for uri in (
        CLAUDE_REDIRECT + "/",
        CLAUDE_REDIRECT + "?x=1",
        "http://claude.ai/api/mcp/auth_callback",
    ):
        assert a.authorize(redirect_uri=uri).status_code == 400, uri


def test_5_wrong_resource_is_refused(live_auth):
    live = live_auth
    a = _ready(live)
    r = a.authorize(resource="https://other.example/mcp")
    assert r.status_code == 302
    q = query_of(r.headers["location"])
    assert (
        r.headers["location"].startswith(CLAUDE_REDIRECT)
        and q["error"] == "invalid_request"
    )
    assert "resource" in q["error_description"]
    assert live.sql("SELECT count(*) AS n FROM oauth_pending")[0]["n"] == 0
    # omitting the resource is allowed; tokens are bound to this endpoint anyway
    r = a.authorize(resource=None)
    assert r.headers["location"].startswith(f"{live.url}/signin")
    code = a.code(
        a.submit(
            a.open_page(r.headers["location"]), mode="signin", email="ref@example.com"
        )
    )
    a.exchange(code)
    assert live.sql(
        "SELECT DISTINCT resource FROM oauth_token WHERE client_id IS NOT NULL"
    ) == [{"resource": live.resource}]


def test_5_expired_and_reused_codes_are_refused(live_auth):
    live = live_auth
    a = _ready(live)
    # expired
    code = a.code(a.sign_in("signin", "ref@example.com"))
    live.sql("UPDATE oauth_code SET expires_at = now() - interval '1 minute'")
    r = a.exchange(code)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    _no_token(live)
    # reused: the second exchange fails, and the tokens of the first die with it
    code = a.code(a.sign_in("signin", "ref@example.com"))
    first = a.exchange(code)
    assert first.status_code == 200
    access = first.json()["access_token"]
    assert a.mcp_status(access) == 200
    again = a.exchange(code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert a.mcp_status(access) == 401
    assert a.refresh(first.json()["refresh_token"]).status_code == 400


def test_5_code_of_another_client_or_redirect_is_refused(live_auth):
    live = live_auth
    a = _ready(live)
    b = Assistant(live, name="Other", redirect_uri=LOOPBACK_REDIRECT)
    b.register()
    code = a.code(a.sign_in("signin", "ref@example.com"))
    r = b.exchange(code, code_verifier=a.verifier)
    assert r.status_code == 400
    r = a.exchange(code, redirect_uri="https://claude.ai/other")
    assert r.status_code == 400
    _no_token(live)


# 6 -----------------------------------------------------------------------------


def test_6_a_new_account_is_unverified_works_and_has_keys_and_identity(live_auth):
    live = live_auth
    a = Assistant(live)
    a.register()
    done = a.sign_in("signup", "fresh@example.com", handle="fresh")
    token = a.exchange(a.code(done)).json()["access_token"]

    row = live.sql("SELECT * FROM account WHERE email = 'fresh@example.com'")[0]
    assert row["verified"] is False and row["verified_at"] is None
    assert row["status"] == "active"
    keys = live.sql(
        "SELECT key_id FROM account_key WHERE account_id = %s ORDER BY key_id",
        (row["id"],),
    )
    assert [k["key_id"] for k in keys] == ["e1", "k1", "root"]
    with connect(live.db_url) as conn:
        from reticula_mcp import records

        head = records.head(conn, row["identity_address"])
        assert head is not None and head.type == "identity"
        assert records.verify_record(conn, head)

    async def work(session, init):
        return (await session.call_tool("start", {})).content[0].text

    text = run_mcp(live, token, work)
    assert not text.startswith("[error:Forbidden]")


# 7 -----------------------------------------------------------------------------


def test_7_unauthenticated_mcp_gets_401_with_the_resource_metadata_challenge(live_auth):
    live = live_auth
    with live.http() as http:
        for headers in (
            {},
            {"Authorization": "Bearer nope"},
            {"Authorization": "Basic abc"},
        ):
            r = http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream", **headers},
            )
            assert r.status_code == 401
            challenge = r.headers["www-authenticate"]
            assert challenge.startswith("Bearer ")
            assert (
                f'resource_metadata="{live.url}/.well-known/oauth-protected-resource/mcp"'
                in challenge
            )
        # both discovery documents are reachable at the advertised places
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            d = http.get(path, headers={"Origin": "https://inspector.example"})
            assert (
                d.status_code == 200 and d.headers["access-control-allow-origin"] == "*"
            )


def test_7_metadata_urls_come_from_the_public_url_not_the_host_header(fresh_db_url):
    with start_live(fresh_db_url, public_url="https://reticula.example.test") as live:
        with live.http() as http:
            meta = http.get(
                "/.well-known/oauth-authorization-server",
                headers={"Host": "evil.example"},
            ).json()
            assert meta["issuer"] == "https://reticula.example.test"
            assert meta["token_endpoint"] == "https://reticula.example.test/token"
            prm = http.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": "evil.example"},
            ).json()
            assert prm["resource"] == "https://reticula.example.test/mcp"
            assert prm["authorization_servers"] == ["https://reticula.example.test"]
            r = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert (
                'resource_metadata="https://reticula.example.test/.well-known/oauth-protected-resource/mcp"'
                in r.headers["www-authenticate"]
            )
            # the page's session cookie is Secure when the public URL is https
            a = Assistant(live)
            a.register()
            loc = a.authorize(resource="https://reticula.example.test/mcp").headers[
                "location"
            ]
            assert loc.startswith("https://reticula.example.test/signin?req=")
            page = a.browser.get(loc.replace("https://reticula.example.test", ""))
            cookie = page.headers["set-cookie"].lower()
            assert (
                "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
            )


def test_expired_session_helper_sanity():
    assert timedelta(hours=1) == config.ACCESS_TTL
