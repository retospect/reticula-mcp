"""Regression tests for the security review of the sign-in lane.

Each ``test_fixed_*`` passes when a reviewed weakness is GONE; the ``test_sound_*``
tests were written by the reviewer for things that already held up.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from argon2 import PasswordHasher
from starlette.requests import Request

from reticula_mcp import accounts
from reticula_mcp.auth import build_auth, config, grants, page
from reticula_mcp.auth.netaddr import address_bucket, client_address
from reticula_mcp.auth.provider import ReticulaProvider
from reticula_mcp.cli import main
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.db.pool import Database
from reticula_mcp.errors import ReticulaError
from tests.auth_support import (
    CLAUDE_REDIRECT,
    LOOPBACK_REDIRECT,
    PASSWORD,
    Assistant,
    form_field,
    pkce,
    start_live,
)


@pytest.fixture
def live_auth(fresh_db_url):
    with start_live(fresh_db_url) as live:
        yield live


def make_account(live, email, handle=None):
    with connect(live.db_url) as conn:
        account = accounts.create_account(conn, email, PASSWORD, handle)
        conn.commit()
    return account


def _request(peer, headers):
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


# 1. revoke racing a refresh rotation -------------------------------------------


def test_fixed_revoke_racing_a_refresh_leaves_no_live_token_pair(
    live_auth, monkeypatch
):
    live = live_auth
    make_account(live, "victim@example.com")
    a = Assistant(live)
    a.connect("signin", "victim@example.com")
    grant_id = live.sql("SELECT grant_id FROM oauth_grant")[0]["grant_id"]

    db = Database(live.db_url)
    db.open()
    provider = ReticulaProvider(live.settings, db)
    stored = provider._load_refresh(a.client_id, a.tokens["refresh_token"])
    assert stored is not None

    orig = grants.issue_pair
    inside, release = threading.Event(), threading.Event()
    result = {}

    def paused_issue_pair(*args, **kw):
        out = orig(*args, **kw)
        inside.set()  # new rows inserted, transaction still open
        release.wait(10)
        return out

    monkeypatch.setattr(grants, "issue_pair", paused_issue_pair)

    def rotate():
        result["tokens"] = provider._exchange_refresh(a.client_id, stored, ["mcp"])

    t_rot = threading.Thread(target=rotate)
    t_rot.start()
    assert inside.wait(10)

    def revoke():
        with db.connection() as conn:
            grants.revoke_grant_key(conn, grant_id)

    t_rev = threading.Thread(target=revoke)
    t_rev.start()
    time.sleep(0.5)  # revoke is blocked behind the rotation's lock on the grant
    release.set()
    t_rot.join(10)
    t_rev.join(10)

    assert live.sql("SELECT revoked_at FROM oauth_grant")[0]["revoked_at"] is not None
    new = result["tokens"]
    if new is not None:  # the rotation won the race: its pair must be dead now
        assert a.mcp_status(new.access_token) == 401
        assert a.refresh(new.refresh_token).status_code == 400
    assert a.mcp_status(a.tokens["access_token"]) == 401
    live_tokens = live.sql(
        "SELECT count(*) AS n FROM oauth_token WHERE revoked_at IS NULL"
    )
    assert live_tokens[0]["n"] == 0
    db.close()


def test_fixed_the_grant_row_is_authoritative_over_token_rows(live_auth):
    live = live_auth
    make_account(live, "auth@example.com")
    a = Assistant(live)
    a.connect("signin", "auth@example.com")
    live.sql("UPDATE oauth_grant SET revoked_at = now()")  # tokens left untouched
    n = live.sql("SELECT count(*) AS n FROM oauth_token WHERE revoked_at IS NULL")
    assert n[0]["n"] == 2
    assert a.mcp_status() == 401
    assert a.refresh().status_code == 400


def test_fixed_revocation_statistics_against_a_refresh_looping_thief(live_auth):
    live = live_auth
    make_account(live, "stat@example.com")
    trials = 12
    a = Assistant(live)
    assert a.register().status_code == 201
    for i in range(trials):
        code = a.code(a.sign_in("signin", "stat@example.com"))
        assert a.exchange(code).status_code == 200
        stop = threading.Event()
        box = {"tokens": dict(a.tokens)}

        def thief(a=a, stop=stop, box=box):
            with live.http() as c:
                while not stop.is_set():
                    r = c.post(
                        "/token",
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": box["tokens"]["refresh_token"],
                            "client_id": a.client_id,
                        },
                    )
                    if r.status_code == 200:
                        box["tokens"] = r.json()
                    else:
                        return

        t = threading.Thread(target=thief)
        t.start()
        time.sleep(0.05 + 0.003 * i)
        a.api.post(
            "/revoke",
            data={"token": a.tokens["access_token"], "client_id": a.client_id},
        )
        stop.set()
        t.join(10)
        n = live.sql(
            "SELECT count(*) AS n FROM oauth_token WHERE kind='access'"
            " AND revoked_at IS NULL AND expires_at > now()"
        )[0]["n"]
        assert n == 0, f"trial {i}: a live access token survived revocation"


# 2. proxy warning --------------------------------------------------------------


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_fixed_untrusted_proxy_start_up_warning(caplog):
    db = Database("postgresql://nowhere/none")  # never opened
    with caplog.at_level(logging.WARNING, logger="reticula_mcp.auth"):
        build_auth(Settings(public_url="https://reticula.example.test"), db)
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert "RETICULA_TRUSTED_PROXIES" in msgs[0] and "ONE rate-limit bucket" in msgs[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="reticula_mcp.auth"):
        build_auth(
            Settings(
                public_url="https://reticula.example.test",
                trusted_proxies=("127.0.0.1",),
            ),
            db,
        )
        build_auth(Settings(), db)  # plain loopback, no proxy
        build_auth(Settings(public_url="http://localhost:8787"), db)
        build_auth(Settings(public_url="http://127.0.0.1:8787"), db)
    assert not _warnings(caplog)

    with caplog.at_level(logging.WARNING, logger="reticula_mcp.auth"):
        build_auth(Settings(public_url="http://tunnel.example.test"), db)
    assert len(_warnings(caplog)) == 1


# 3. the page names the receiver, not the self-given name -----------------------


def _page_for(live, name, redirect):
    evil = Assistant(live, name=name, redirect_uri=redirect)
    assert evil.register().status_code == 201
    link = evil.authorize().headers["location"]
    return httpx.Client(base_url=live.url).get(
        f"{urlsplit(link).path}?{urlsplit(link).query}"
    )


def test_fixed_lookalike_client_is_flagged_on_the_page(live_auth):
    text = _page_for(live_auth, "Claude", "https://claude-ai.example/cb").text
    assert "<strong>claude-ai.example</strong> gets access to your notes" in text
    assert "calls itself" in text and "Claude" in text
    assert text.index("claude-ai.example") < text.index("calls itself")
    assert 'class="warn"' in text and "Unfamiliar address" in text
    assert "never because of a link someone sent you" in text
    assert "You should only see this page right after you add this server" in text
    assert "<script" not in text


def test_page_known_host_and_loopback_wording(live_auth):
    known = _page_for(live_auth, "Claude", CLAUDE_REDIRECT).text
    assert "<strong>claude.ai</strong> gets access" in known
    assert 'class="warn"' not in known
    assert "You should only see this page right after you add this server" in known
    local = _page_for(live_auth, "Inspector", LOOPBACK_REDIRECT).text
    assert "an application on this computer</strong> (port 53682)" in local
    assert 'class="warn"' not in local
    assert "You should only see this page right after you add this server" in local


def _render(redirect_uri, known_hosts):
    return page.sign_in_page(
        handle="h",
        csrf="c",
        mode="signin",
        client_name="X",
        redirect_uri=redirect_uri,
        signups_open=True,
        known_hosts=known_hosts,
    ).body.decode()


def test_known_hosts_come_from_settings_when_the_field_exists():
    assert config.known_clients(Settings()) == ("claude.ai", "claude.com")
    other = SimpleNamespace(known_clients=("Example.org",))
    assert config.known_clients(other) == ("example.org",)
    html = _render("https://example.org/cb", ("example.org",))
    assert 'class="warn"' not in html and "<strong>example.org</strong>" in html
    # a look-alike subdomain of a known host is not known
    assert 'class="warn"' in _render(
        "https://claude.ai.evil.example/cb", ("claude.ai",)
    )


def test_page_csp_matches_the_inline_style():
    digest = base64.b64encode(hashlib.sha256(page.STYLE.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in page.CSP
    assert "default-src 'none'" in page.CSP and "frame-ancestors 'none'" in page.CSP


# 4/5. lockout races and lockout as denial of service ---------------------------


def test_fixed_concurrent_guesses_do_not_get_past_the_per_email_lockout(
    live_auth, monkeypatch
):
    monkeypatch.setattr(accounts, "PASSWORD_HASHER", PasswordHasher())  # prod cost
    monkeypatch.setattr(accounts, "_dummy_hash", None)
    live = live_auth
    make_account(live, "target@example.com")
    a = Assistant(live)
    assert a.register().status_code == 201
    auth = a.authorize()
    page_ = a.open_page(auth.headers["location"], "signin")
    cookies = dict(a.browser.cookies)
    data = {
        "req": form_field(page_.text, "req"),
        "csrf": form_field(page_.text, "csrf"),
        "mode": "signin",
        "email": "target@example.com",
        "password": "wrong password number",
    }

    def attempt(_):
        with live.http(cookies=cookies) as c:
            return c.post("/signin", data=data).status_code

    with ThreadPoolExecutor(24) as pool:
        codes = list(pool.map(attempt, range(24)))
    assert codes.count(401) == config.FAILURES_PER_EMAIL, sorted(codes)
    assert codes.count(429) == 24 - config.FAILURES_PER_EMAIL
    rows = live.sql("SELECT count(*) AS n FROM auth_event WHERE kind='signin_email'")
    assert rows[0]["n"] == config.FAILURES_PER_EMAIL


def test_fixed_a_successful_sign_in_takes_its_provisional_record_back(live_auth):
    live = live_auth
    make_account(live, "ok@example.com")
    a = Assistant(live)
    a.register()
    assert a.sign_in("signin", "ok@example.com").status_code == 303
    rows = live.sql("SELECT count(*) AS n FROM auth_event WHERE kind LIKE 'signin%'")
    assert rows[0]["n"] == 0


def test_fixed_guessing_from_another_address_cannot_lock_the_owner_out(fresh_db_url):
    with start_live(fresh_db_url, trusted_proxies=("127.0.0.1",)) as live:
        make_account(live, "victim2@example.com")
        attacker = Assistant(live)
        attacker.register()
        attacker.browser.headers["X-Forwarded-For"] = "203.0.113.50"
        for _ in range(config.FAILURES_PER_EMAIL):
            r = attacker.sign_in("signin", "victim2@example.com", "wrong wrong wrong")
            assert r.status_code == 401
        locked = attacker.sign_in("signin", "victim2@example.com", "wrong wrong wrong")
        assert locked.status_code == 429
        owner = Assistant(live, name="Claude")
        owner.register()
        owner.browser.headers["X-Forwarded-For"] = "198.51.100.77"
        assert (
            owner.sign_in("signin", "victim2@example.com", PASSWORD).status_code == 303
        )


# 6. X-Forwarded-For parsing ------------------------------------------------------


def test_fixed_every_xff_header_line_is_read_and_the_right_end_wins():
    trusting = Settings(trusted_proxies=("10.0.0.0/8",))
    req = _request(
        "10.0.0.5",
        [(b"x-forwarded-for", b"1.2.3.4"), (b"x-forwarded-for", b"198.51.100.9")],
    )
    assert client_address(req, trusting) == "198.51.100.9"
    req = _request(
        "10.0.0.5",
        [
            (b"x-forwarded-for", b"1.2.3.4, 198.51.100.9"),
            (b"x-forwarded-for", b"10.0.0.7"),
        ],
    )
    assert client_address(req, trusting) == "198.51.100.9"


def test_fixed_ipv4_mapped_addresses_are_normalised():
    trusting = Settings(trusted_proxies=("127.0.0.1",))
    req = _request("::ffff:127.0.0.1", [(b"x-forwarded-for", b"198.51.100.9")])
    assert client_address(req, trusting) == "198.51.100.9"
    req = _request("127.0.0.1", [(b"x-forwarded-for", b"::ffff:198.51.100.9")])
    assert client_address(req, trusting) == "198.51.100.9"
    assert client_address(_request("::ffff:8.8.8.8", []), trusting) == "8.8.8.8"
    assert address_bucket("::ffff:8.8.8.8") == "8.8.8.8"


# 7. malformed input -> 400, not 500 ----------------------------------------------


def test_fixed_nul_byte_in_the_form_is_a_400(live_auth):
    live = live_auth
    a = Assistant(live)
    a.register()
    page_ = a.open_page(a.authorize().headers["location"], "signup")
    for field in ("email", "password", "handle"):
        data = {
            "req": form_field(page_.text, "req"),
            "csrf": form_field(page_.text, "csrf"),
            "mode": "signup",
            "email": "a@example.com",
            "password": "whatever whatever",
            "handle": "fine",
            field: "a\x00b",
        }
        r = a.browser.post("/signin", data=data)
        assert r.status_code == 400, field
    n = live.sql("SELECT count(*) AS n FROM account WHERE email LIKE 'a%'")
    assert n[0]["n"] == 0


def test_fixed_too_many_form_fields_is_a_400(live_auth):
    r = httpx.post(f"{live_auth.url}/signin", data={f"f{i}": "x" for i in range(13)})
    assert r.status_code == 400


# 8. pending-request quota ------------------------------------------------------


def test_fixed_one_address_cannot_exhaust_a_shared_clients_pending_quota(
    fresh_db_url,
):
    with start_live(fresh_db_url, trusted_proxies=("127.0.0.1",)) as live:
        victim_app = Assistant(live, name="Claude")
        assert victim_app.register().status_code == 201
        flood = httpx.Client(base_url=live.url, follow_redirects=False, timeout=30)
        refused = 0
        for _ in range(config.MAX_PENDING_PER_ADDRESS + 10):
            _, challenge = pkce()
            r = flood.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": victim_app.client_id,
                    "redirect_uri": CLAUDE_REDIRECT,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
            assert r.status_code == 302
            refused += "temporarily_unavailable" in r.headers["location"]
        assert refused == 10
        n = live.sql("SELECT count(*) AS n FROM oauth_pending")[0]["n"]
        assert n == config.MAX_PENDING_PER_ADDRESS < config.MAX_PENDING_PER_CLIENT
        # a real user at another address is not affected
        victim_app.browser.headers["X-Forwarded-For"] = "198.51.100.1"
        r = victim_app.authorize()
        assert r.headers["location"].startswith(f"{live.url}/signin?req=")


# 9. operator tokens ------------------------------------------------------------


def test_fixed_operator_tokens_are_listed_and_revocable(live_auth, capsys, monkeypatch):
    live = live_auth
    acct = make_account(live, "op@example.com")
    with connect(live.db_url) as conn:
        token = accounts.mint_token(conn, acct, "laptop")
        accounts.mint_token(conn, acct, "phone")
        conn.commit()
    a = Assistant(live)
    a.connect("signin", "op@example.com")
    monkeypatch.setenv("RETICULA_DATABASE_URL", live.db_url)
    assert a.mcp_status(token) == 200

    who = ["--email", "op@example.com"]
    assert main(["connections", "list", *who]) == 0
    out = capsys.readouterr().out
    assert "operator tokens (2)" in out and "laptop" in out and "phone" in out
    tid = live.sql("SELECT id FROM oauth_token WHERE label = 'laptop'")[0]["id"]
    assert main(["connections", "revoke", *who, "--token", str(tid)]) == 0
    assert "revoked operator token" in capsys.readouterr().out
    assert a.mcp_status(token) == 401
    assert a.mcp_status() == 200  # the OAuth connection is untouched
    assert main(["connections", "list", *who]) == 0
    assert "revoked" in capsys.readouterr().out
    # another account's token, an OAuth token, and no/both selectors are refused
    other = make_account(live, "other@example.com")
    with connect(live.db_url) as conn:
        accounts.mint_token(conn, other, "theirs")
        conn.commit()
    theirs = live.sql("SELECT id FROM oauth_token WHERE label = 'theirs'")[0]["id"]
    oauth_id = live.sql("SELECT id FROM oauth_token WHERE client_id IS NOT NULL")[0][
        "id"
    ]
    for bad in (theirs, oauth_id):
        assert main(["connections", "revoke", *who, "--token", str(bad)]) == 1
    assert main(["connections", "revoke", *who]) == 1
    assert main(["connections", "revoke", *who, "--id", "1", "--token", "2"]) == 1


# 10. housekeeping --------------------------------------------------------------


def test_fixed_purge_drops_spent_rows_but_keeps_live_and_operator_ones(live_auth):
    live = live_auth
    acct = make_account(live, "purge@example.com")
    with connect(live.db_url) as conn:
        accounts.mint_token(conn, acct, "operator")
        conn.commit()
    a = Assistant(live)
    a.connect("signin", "purge@example.com")
    live.sql(
        "UPDATE oauth_token SET consumed_at = now() - interval '40 days',"
        " expires_at = now() - interval '5 days' WHERE kind = 'refresh'"
    )
    live.sql("UPDATE oauth_code SET expires_at = now() - interval '5 days'")
    live.sql(
        "INSERT INTO oauth_token (token_hash, kind, client_id, account_id, expires_at)"
        " VALUES ('fresh-refresh', 'refresh', %s, %s, now() + interval '20 days')",
        (a.client_id, acct.id),
    )
    with connect(live.db_url) as conn:
        grants.purge_stale(conn)
        conn.commit()
    kinds = live.sql(
        "SELECT kind, client_id IS NULL AS op, token_hash FROM oauth_token ORDER BY id"
    )
    assert [(k["kind"], k["op"]) for k in kinds] == [
        ("access", True),  # operator token: never purged
        ("access", False),  # the live access token
        ("refresh", False),  # only the live refresh token is left
    ]
    assert kinds[2]["token_hash"] == "fresh-refresh"
    assert live.sql("SELECT count(*) AS n FROM oauth_code")[0]["n"] == 0


def test_fixed_indexes_exist(live_auth):
    names = {r["indexname"] for r in live_auth.sql("SELECT indexname FROM pg_indexes")}
    assert {
        "oauth_token_client_idx",
        "oauth_code_client_idx",
        "oauth_grant_client_idx",
        "oauth_pending_client_idx",
        "oauth_pending_ip_idx",
    } <= names


def test_fixed_password_minimum_is_ten_everywhere(conn):
    assert accounts.MIN_PASSWORD_LENGTH == config.MIN_PASSWORD_LENGTH == 10
    with pytest.raises(ReticulaError):
        accounts.create_account(conn, "nine@example.com", "123456789")
    accounts.create_account(conn, "ten@example.com", "1234567890")


# --- things that held up (reviewer's tests, unchanged) ---------------------------


def test_sound_token_endpoint_refuses_huge_bodies(live_auth):
    big = "x" * (20 * 1024 * 1024)
    r = httpx.post(
        f"{live_auth.url}/token",
        data={"grant_type": "authorization_code", "client_id": "nope", "pad": big},
        timeout=60,
    )
    assert r.status_code == 413


def test_sound_concurrent_code_exchange_has_one_winner_and_replay_kills_it(
    live_auth,
):
    live = live_auth
    make_account(live, "race@example.com")
    a = Assistant(live)
    a.register()
    code = a.code(a.sign_in("signin", "race@example.com"))
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": a.redirect_uri,
        "client_id": a.client_id,
        "code_verifier": a.verifier,
    }

    def go(_):
        with live.http() as c:
            return c.post("/token", data=data)

    with ThreadPoolExecutor(8) as pool:
        rs = list(pool.map(go, range(8)))
    for r in [r for r in rs if r.status_code == 200]:
        assert a.mcp_status(r.json()["access_token"]) == 401
    live_tokens = live.sql(
        "SELECT count(*) AS n FROM oauth_token WHERE revoked_at IS NULL"
    )
    assert live_tokens[0]["n"] == 0


def test_sound_disabled_account_loses_access_everywhere(live_auth):
    live = live_auth
    make_account(live, "gone@example.com")
    a = Assistant(live)
    a.connect("signin", "gone@example.com")
    old_refresh = a.tokens["refresh_token"]
    live.sql("UPDATE account SET status='disabled' WHERE email='gone@example.com'")
    assert a.mcp_status() == 401
    assert a.refresh(old_refresh).status_code == 400
    b = Assistant(live)
    b.register()
    assert b.sign_in("signin", "gone@example.com").status_code == 401


def test_sound_stateful_session_cannot_be_used_by_another_account(fresh_db_url):
    with start_live(fresh_db_url, stateless_http=False) as live:
        ta = make_account(live, "a@example.com")
        tb = make_account(live, "b@example.com")
        with connect(live.db_url) as conn:
            tok_a = accounts.mint_token(conn, ta, "a")
            tok_b = accounts.mint_token(conn, tb, "b")
            conn.commit()
        hdr = {"Accept": "application/json, text/event-stream"}
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        with live.http() as c:
            r = c.post(
                "/mcp", json=init, headers={**hdr, "Authorization": f"Bearer {tok_a}"}
            )
            assert r.status_code == 200, r.text
            sid = r.headers["mcp-session-id"]
            r2 = c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={
                    **hdr,
                    "Authorization": f"Bearer {tok_b}",
                    "mcp-session-id": sid,
                },
            )
            assert r2.status_code in (400, 401, 403, 404), r2.status_code


def test_sound_host_allow_list_applies_to_mcp_only(fresh_db_url):
    with start_live(fresh_db_url) as live:
        acct = make_account(live, "h@example.com")
        with connect(live.db_url) as conn:
            tok = accounts.mint_token(conn, acct, "h")
            conn.commit()
        hdr = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        with live.http() as c:
            evil = c.post("/mcp", json=body, headers={**hdr, "Host": "evil.example"})
            assert evil.status_code == 421
            r = c.post(
                "/register",
                json={"redirect_uris": ["https://a.example/c"]},
                headers={"Host": "evil.example"},
            )
            assert r.status_code == 201
