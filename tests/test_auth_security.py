"""Sign-in lane: refusal tests. Every request parameter is treated as hostile."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from reticula_mcp import accounts
from reticula_mcp.auth import config, page, verification
from reticula_mcp.auth.netaddr import address_bucket, client_address
from reticula_mcp.auth.registration import clean_name, validate_redirect_uri
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from tests.auth_support import (
    CLAUDE_REDIRECT,
    LOOPBACK_REDIRECT,
    PASSWORD,
    Assistant,
    form_field,
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


# registration -----------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/cb",
        "ftp://claude.ai/cb",
        "javascript:alert(1)",
        "https://claude.ai/cb#frag",
        "https://user:pw@claude.ai/cb",
        "https://claude.ai/cb\\@evil.example",
        "https://claude.ai/ cb",
        "https:///nohost",
        "http://localhost.evil.example/cb",
        "https://claude.ai:99999/cb",
        "x" * 3000,
        "",
        None,
        7,
    ],
)
def test_redirect_uris_that_are_not_https_or_loopback_are_refused(uri, live_auth):
    with pytest.raises(ValueError):
        validate_redirect_uri(uri)
    a = Assistant(live_auth, redirect_uri="https://claude.ai/ok")
    r = a.register(redirect_uris=[uri])
    assert r.status_code == 400
    assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


@pytest.mark.parametrize(
    "uri",
    [
        CLAUDE_REDIRECT,
        "http://localhost:6274/oauth/callback",
        "http://127.0.0.1:53682/callback",
        "http://[::1]:8080/cb",
    ],
)
def test_https_and_loopback_redirect_uris_are_accepted(uri):
    assert validate_redirect_uri(uri) == uri


def test_registration_is_limited_in_size_count_and_rate(fresh_db_url, monkeypatch):
    monkeypatch.setenv("RETICULA_REGISTER_RATE", "4/hour")
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        big = a.api.post("/register", content=b'{"x":"' + b"a" * 20000 + b'"}')
        assert big.status_code == 413
        assert a.api.post("/register", content=b"not json").status_code == 400
        assert a.api.post("/register", json=[1]).status_code == 400
        many = a.register(redirect_uris=[f"https://c.example/{i}" for i in range(11)])
        assert many.status_code == 400
        # the four requests above used the allowance (invalid ones count too)
        assert a.register().status_code == 429
        assert a.register().headers["retry-after"]
        assert live.sql("SELECT count(*) AS n FROM oauth_client")[0]["n"] == 0


def test_registered_client_is_public_and_the_name_is_sanitised(live_auth):
    a = Assistant(live_auth, name="Evil‮<b>x</b>\x00\n  Name" + "z" * 300)
    r = a.register(token_endpoint_auth_method="client_secret_post", scope="admin")
    assert r.status_code == 201
    info = r.json()
    assert info["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in info
    assert info["scope"] == "mcp"
    assert "‮" not in info["client_name"] and "\x00" not in info["client_name"]
    assert len(info["client_name"]) <= config.CLIENT_NAME_LENGTH
    assert clean_name(None) == "an assistant"


def test_client_name_is_html_escaped_on_the_page(live_auth):
    a = Assistant(live_auth, name='<script>alert(1)</script> & "q"')
    a.register()
    page_ = a.open_page(a.authorize().headers["location"])
    assert "<script>" not in page_.text
    assert "&lt;script&gt;" in page_.text


# the page ---------------------------------------------------------------------


def test_page_has_security_headers_and_no_scripts_or_assets(live_auth):
    a = Assistant(live_auth)
    a.register()
    p = a.open_page(a.authorize().headers["location"])
    h = p.headers
    assert "default-src 'none'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "base-uri 'none'" in h["content-security-policy"]
    assert "no-store" in h["cache-control"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"
    assert (
        "<script" not in p.text
        and "src=" not in p.text
        and "http" not in p.text.split("<body>")[1].replace("claude.ai", "")
    )
    assert 'name="viewport"' in p.text
    assert 'autocomplete="current-password"' in p.text
    signup = a.open_page(a.authorize().headers["location"], "signup")
    assert 'autocomplete="new-password"' in signup.text


def test_post_without_matching_csrf_is_refused_and_keeps_the_request(live_auth):
    live = live_auth
    make_account(live, "csrf@example.com")
    a = Assistant(live)
    a.register()
    loc = a.authorize().headers["location"]
    p = a.open_page(loc)
    for tamper in ({"csrf": "wrong"}, {"csrf": ""}):
        r = a.submit(p, mode="signin", email="csrf@example.com", **tamper)
        assert r.status_code == 403 and "location" not in r.headers
    # no cookie (another browser) with the right form value
    other = live.http()
    r = other.post(
        "/signin",
        data={
            "req": form_field(p.text, "req"),
            "csrf": form_field(p.text, "csrf"),
            "mode": "signin",
            "email": "csrf@example.com",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 403
    assert live.sql("SELECT count(*) AS n FROM oauth_code")[0]["n"] == 0
    # the legitimate submit still works afterwards
    assert a.submit(p, mode="signin", email="csrf@example.com").status_code == 303


def test_a_csrf_pair_from_another_parked_request_is_refused(live_auth):
    live = live_auth
    make_account(live, "x@example.com")
    a, b = Assistant(live), Assistant(live)
    a.register()
    b.register()
    pa = a.open_page(a.authorize().headers["location"])
    pb = b.open_page(b.authorize().headers["location"])
    r = a.browser.post(
        "/signin",
        data={
            "req": form_field(pa.text, "req"),
            "csrf": form_field(pb.text, "csrf"),
            "mode": "signin",
            "email": "x@example.com",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 403


def test_a_parked_request_is_single_use_and_unknown_handles_are_refused(live_auth):
    live = live_auth
    make_account(live, "once@example.com")
    a = Assistant(live)
    a.register()
    p = a.open_page(a.authorize().headers["location"])
    assert a.submit(p, mode="signin", email="once@example.com").status_code == 303
    again = a.submit(p, mode="signin", email="once@example.com")
    assert again.status_code == 400 and "location" not in again.headers
    for req in ("nope", "A" * 43, "../../etc/passwd", "x" * 5000):
        assert a.browser.get("/signin", params={"req": req}).status_code == 400
    assert a.browser.get("/signin").status_code == 400


def test_the_redirect_target_cannot_be_steered_by_the_form(live_auth):
    live = live_auth
    make_account(live, "steer@example.com")
    a = Assistant(live)
    a.register()
    p = a.open_page(a.authorize().headers["location"])
    r = a.submit(
        p,
        mode="signin",
        email="steer@example.com",
        redirect_uri="https://evil.example/cb",
        client_id="other",
        state="forged",
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith(CLAUDE_REDIRECT + "?")
    assert "evil" not in r.headers["location"]


def test_a_tampered_parked_redirect_is_never_followed(live_auth):
    live = live_auth
    make_account(live, "tamper@example.com")
    a = Assistant(live)
    a.register()
    p = a.open_page(a.authorize().headers["location"])
    live.sql(
        "UPDATE oauth_pending SET params = jsonb_set(params, '{redirect_uri}',"
        " '\"https://evil.example/cb\"')"
    )
    r = a.submit(p, mode="signin", email="tamper@example.com")
    assert r.status_code == 400 and "location" not in r.headers
    assert live.sql("SELECT count(*) AS n FROM oauth_code")[0]["n"] == 0


def test_form_body_is_limited_and_must_be_urlencoded(live_auth):
    a = Assistant(live_auth)
    a.register()
    p = a.open_page(a.authorize().headers["location"])
    big = a.browser.post(
        "/signin",
        content=b"req=x&password=" + b"a" * 20000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert big.status_code == 413
    js = a.browser.post("/signin", json={"req": "x"})
    assert js.status_code == 415


def test_create_account_rules_and_messages(fresh_db_url):
    with start_live(fresh_db_url, signup_rate="100/hour") as live:
        _create_account_rules(live)


def _create_account_rules(live):
    make_account(live, "taken@example.com", handle="taken")
    a = Assistant(live)
    a.register()

    r = a.sign_in("signup", "short@example.com", "short")
    assert r.status_code == 400 and "at least 10" in r.text
    r = a.sign_in("signup", "taken@example.com")
    assert r.status_code == 409 and "already registered" in r.text
    r = a.sign_in("signup", "other@example.com", handle="taken")
    assert r.status_code == 409 and "taken" in r.text
    r = a.sign_in("signup", "other@example.com", handle="admin")
    assert r.status_code == 400
    r = a.sign_in("signup", "not-an-email")
    assert r.status_code == 400
    r = a.sign_in("signup", "<b>x</b>@example.com")
    assert "<b>x</b>" not in r.text
    assert PASSWORD not in r.text
    assert (
        live.sql("SELECT count(*) AS n FROM account WHERE email LIKE 'other%'")[0]["n"]
        == 0
    )
    # a failed create leaves the parked request usable
    p = a.open_page(a.authorize().headers["location"], "signup")
    bad = a.submit(p, mode="signup", email="taken@example.com")
    assert bad.status_code == 409
    again = a.submit(p, mode="signup", email="fine@example.com", handle="fine")
    assert again.status_code == 303


def test_sign_in_mode_never_says_whether_an_email_exists(live_auth):
    live = live_auth
    make_account(live, "exists@example.com")
    a = Assistant(live)
    a.register()
    known = a.sign_in("signin", "exists@example.com", "wrong password here")
    unknown = a.sign_in("signin", "ghost@example.com", "wrong password here")
    assert known.status_code == unknown.status_code == 401

    def body(r):
        text = r.text
        for field in ("req", "csrf"):
            text = text.replace(form_field(text, field), "")
        return text.replace("exists@example.com", "").replace("ghost@example.com", "")

    assert body(known) == body(unknown)


def test_unknown_mode_values_fall_back_to_sign_in(live_auth):
    live = live_auth
    make_account(live, "m@example.com")
    a = Assistant(live)
    a.register()
    p = a.open_page(a.authorize().headers["location"])
    r = a.submit(p, mode="admin", email="m@example.com")
    assert r.status_code == 303


# token endpoint / revoke --------------------------------------------------------


def test_revoke_with_unknown_client_or_foreign_token_does_nothing(live_auth):
    live = live_auth
    make_account(live, "rv@example.com")
    a = Assistant(live)
    a.connect("signin", "rv@example.com")
    b = Assistant(live, name="B", redirect_uri=LOOPBACK_REDIRECT)
    b.register()
    # B (a different client) cannot revoke A's token
    assert (
        b.api.post(
            "/revoke",
            data={"token": a.tokens["access_token"], "client_id": b.client_id},
        ).status_code
        == 200
    )
    assert a.mcp_status() == 200
    assert (
        b.api.post("/revoke", data={"token": "x", "client_id": "nope"}).status_code
        == 401
    )
    assert b.api.post("/revoke", data={"token": "x"}).status_code == 400
    # revoking an access token revokes the grant (refresh token too)
    a.api.post(
        "/revoke", data={"token": a.tokens["access_token"], "client_id": a.client_id}
    )
    assert a.mcp_status() == 401
    assert a.refresh().status_code == 400


def test_only_digests_of_secrets_are_stored(live_auth):
    live = live_auth
    make_account(live, "dg@example.com")
    a = Assistant(live)
    a.register()
    code = a.code(a.sign_in("signin", "dg@example.com"))
    a.exchange(code)
    dump = (
        str(live.sql("SELECT * FROM oauth_code"))
        + str(live.sql("SELECT * FROM oauth_token"))
        + str(live.sql("SELECT * FROM oauth_pending"))
    )
    for secret in (code, a.tokens["access_token"], a.tokens["refresh_token"]):
        assert secret not in dump


def test_token_endpoint_refuses_garbage(live_auth):
    a = Assistant(live_auth)
    a.register()
    for data in (
        {},
        {"grant_type": "password", "client_id": a.client_id},
        {"grant_type": "authorization_code", "client_id": a.client_id},
        {
            "grant_type": "authorization_code",
            "client_id": a.client_id,
            "code": "x" * 9999,
            "code_verifier": "v" * 50,
        },
        {
            "grant_type": "refresh_token",
            "client_id": a.client_id,
            "refresh_token": "rtr_" + "a" * 40,
        },
    ):
        r = a.api.post("/token", data=data)
        assert r.status_code in (400, 401), data


# units ------------------------------------------------------------------------


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_forwarded_for_is_believed_only_from_trusted_peers():
    none = Settings()
    trusting = Settings(trusted_proxies=("127.0.0.1", "10.0.0.0/8"))
    assert client_address(_request("127.0.0.1", "1.2.3.4"), none) == "127.0.0.1"
    assert client_address(_request("8.8.8.8", "1.2.3.4"), trusting) == "8.8.8.8"
    assert client_address(_request("127.0.0.1", "1.2.3.4"), trusting) == "1.2.3.4"
    # walks from the right past trusted hops; the left part is client-supplied
    assert (
        client_address(_request("127.0.0.1", "9.9.9.9, 1.2.3.4, 10.1.1.1"), trusting)
        == "1.2.3.4"
    )
    assert client_address(_request("127.0.0.1", "garbage"), trusting) == "127.0.0.1"
    assert client_address(_request("127.0.0.1"), trusting) == "127.0.0.1"


def test_ipv6_folds_to_its_64_and_mapped_addresses_to_ipv4():
    assert address_bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64"
    assert address_bucket("2001:db8:1:2::1") == address_bucket("2001:db8:1:2:ffff::9")
    assert address_bucket("::ffff:1.2.3.4") == "1.2.3.4"
    assert address_bucket("1.2.3.4") == "1.2.3.4"


def test_email_verification_is_switched_off_and_documented():
    assert verification.ENABLED is False
    assert "provisional" in (verification.__doc__ or "")
    account = accounts.Account(
        1, "p", "e@x.y", "h", "a", False, None, "active", None, None
    )
    verification.require_verified(account, "invite")  # no-op while off
    assert page.CSP.startswith("default-src 'none'")
