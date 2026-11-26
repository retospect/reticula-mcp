"""The account pages (auth/account_web.py): web sign-in, sessions, CSRF, the
shared throttles, revoke behind a confirm step, settings, export, delete."""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from reticula_mcp import accounts
from reticula_mcp import format as fmt
from reticula_mcp.auth import config, signin, websession
from reticula_mcp.db import connect
from reticula_mcp.graph import handlers as graph
from tests.auth_support import PASSWORD, Assistant, start_live
from tests.graph_support import mk_hub, mk_note, share

COOKIE = "reticula_session"  # plain http on loopback; __Host- with https


def _value(html: str, name: str) -> str:
    found = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert found, name
    return found.group(1)


class Browser:
    def __init__(self, url: str) -> None:
        self.url = url
        self.http = httpx.Client(base_url=url, follow_redirects=False, timeout=30)

    def post(self, path: str, data: dict[str, str], **kw: Any) -> httpx.Response:
        headers = {"Origin": self.url, **kw.pop("headers", {})}
        return self.http.post(path, data=data, headers=headers, **kw)

    def sign_in(
        self, email: str, password: str = PASSWORD, keep: bool = False
    ) -> httpx.Response:
        page = self.http.get("/account/signin")
        data = {"form": _value(page.text, "form"), "email": email, "password": password}
        if keep:
            data["keep"] = "yes"
        return self.post("/account/signin", data)

    def csrf(self, path: str = "/account") -> str:
        return _value(self.http.get(path).text, "csrf")


@pytest.fixture
def site(fresh_db_url: str) -> Iterator[tuple[Any, Any]]:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    with start_live(fresh_db_url) as live:
        yield live, ana


@contextmanager
def db(url: str) -> Iterator[Any]:
    with connect(url) as conn:
        yield conn
        conn.commit()


def test_sign_in_sets_a_strict_cookie_and_shows_the_account(site: Any) -> None:
    live, ana = site
    b = Browser(live.url)
    r = b.sign_in("ana@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/account"
    cookie = next(
        c for c in r.headers.get_list("set-cookie") if c.startswith(f"{COOKIE}=")
    )
    assert cookie.startswith(f"{COOKIE}=")
    for flag in ("HttpOnly", "Path=/", "SameSite=strict"):
        assert flag.lower() in cookie.lower(), flag
    assert "max-age" not in cookie.lower()  # without the box: a browser-session cookie
    page = b.http.get("/account")
    assert page.status_code == 200 and "@ana" in page.text
    assert "Signed in until" in page.text and "30 minutes without use" in page.text
    assert "form-action 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    with db(live.db_url) as conn:
        stored = conn.execute("SELECT id_hash FROM web_session").fetchall()
    token = b.http.cookies[COOKIE]
    assert [s["id_hash"] for s in stored] == [websession.digest(token)]  # digest only


def test_keep_me_signed_in_lasts_30_days(site: Any) -> None:
    live, _ = site
    b = Browser(live.url)
    r = b.sign_in("ana@example.com", keep=True)
    cookie = next(
        c for c in r.headers.get_list("set-cookie") if c.startswith(f"{COOKIE}=")
    )
    assert f"max-age={30 * 24 * 3600}" in cookie.lower()
    with db(live.db_url) as conn:
        conn.execute("UPDATE web_session SET last_seen_at = now() - interval '2 days'")
    assert b.http.get("/account").status_code == 200  # the idle limit is 30 days too


def test_idle_and_absolute_ends(site: Any) -> None:
    live, _ = site
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    with db(live.db_url) as conn:
        conn.execute(
            "UPDATE web_session SET last_seen_at = now() - interval '31 minutes'"
        )
    assert b.http.get("/account").headers["location"] == "/account/signin"
    b.sign_in("ana@example.com")
    with db(live.db_url) as conn:
        conn.execute("UPDATE web_session SET expires_at = now() - interval '1 second'")
    assert b.http.get("/account").headers["location"] == "/account/signin"


def test_each_sign_in_makes_a_new_session(site: Any) -> None:
    """A session id sent before sign-in is never taken on: each sign-in sets a
    new one."""
    live, _ = site
    b = Browser(live.url)

    def issued(r: httpx.Response) -> str:
        cookie = next(
            c for c in r.headers.get_list("set-cookie") if c.startswith(f"{COOKIE}=")
        )
        return cookie.split(";", 1)[0].split("=", 1)[1]

    b.http.cookies.set(COOKIE, "planted-by-someone-else")
    first = issued(b.sign_in("ana@example.com"))
    assert first != "planted-by-someone-else"
    b.http.cookies.clear()
    second = issued(b.sign_in("ana@example.com"))
    assert second != first


def test_wrong_password_and_the_counters_shared_with_the_connector_page(
    site: Any,
) -> None:
    live, _ = site
    b = Browser(live.url)
    r = b.sign_in("ana@example.com", "wrong password here")
    assert r.status_code == 401 and "do not match" in r.text
    with db(live.db_url) as conn:
        rows = conn.execute(
            "SELECT subject FROM auth_event WHERE kind = 'signin_email'"
        ).fetchall()
    assert [r["subject"].split("|")[0] for r in rows] == ["ana@example.com"]
    # the same counter as /signin: after the limit, the connector page refuses too
    for _ in range(config.FAILURES_PER_EMAIL):
        b.sign_in("ana@example.com", "wrong password here")
    a = Assistant(live)
    a.register()
    page = a.open_page(a.authorize().headers["location"])
    assert a.submit(page, mode="signin", email="ana@example.com").status_code == 429
    a.close()


def test_the_password_check_uses_the_shared_hash_slots(
    site: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, _ = site
    used = []

    class Counting:
        def __enter__(self) -> None:
            used.append(1)

        def __exit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr(signin, "hash_slots", Counting())
    Browser(live.url).sign_in("ana@example.com")
    assert used == [1]


@pytest.mark.parametrize(
    "case", ["no token", "wrong token", "foreign origin", "no origin or referer"]
)
def test_a_post_without_the_right_token_or_origin_is_refused(
    site: Any, case: str
) -> None:
    live, _ = site
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    data = {"csrf": b.csrf(), "listed": "no"}
    headers: dict[str, str] = {}
    if case == "no token":
        del data["csrf"]
    elif case == "wrong token":
        data["csrf"] = "0" * 64
    elif case == "foreign origin":
        headers = {"Origin": "https://elsewhere.example"}
    if case == "no origin or referer":
        r = b.http.post("/account/settings", data=data)
    else:
        r = b.post("/account/settings", data, headers=headers)
    assert r.status_code == 403
    with db(live.db_url) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM account_setting").fetchone()["n"]
            == 0
        )


def test_settings_change_listed(site: Any) -> None:
    live, ana = site
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    assert (
        b.post("/account/settings", {"csrf": b.csrf(), "listed": "no"}).status_code
        == 303
    )
    with db(live.db_url) as conn:
        from reticula_mcp import settings

        assert settings.listed(conn, ana) is False


def test_revoke_needs_the_confirm_step_and_a_recent_password(site: Any) -> None:
    live, _ = site
    a = Assistant(live)
    a.connect("signin", "ana@example.com")
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    page = b.http.get("/account").text
    link = re.search(r'href="(/account/revoke/connection/\d+)"', page)
    assert link
    confirm = b.http.get(link.group(1))
    assert "Revoke" in confirm.text and "sign in again" in confirm.text
    assert a.mcp_status() == 200  # nothing yet: GET only asks
    # more than 10 minutes after the password: it is asked again
    with db(live.db_url) as conn:
        conn.execute("UPDATE web_session SET reauth_at = now() - interval '11 minutes'")
    confirm = b.http.get(link.group(1))
    assert 'name="password"' in confirm.text
    csrf = _value(confirm.text, "csrf")
    assert (
        b.post(
            link.group(1), {"csrf": csrf, "password": "wrong password here"}
        ).status_code
        == 401
    )
    assert a.mcp_status() == 200
    assert (
        b.post(link.group(1), {"csrf": csrf, "password": PASSWORD}).status_code == 303
    )
    assert a.mcp_status() == 401
    a.close()


def test_sign_out_and_sign_out_everywhere(site: Any) -> None:
    live, _ = site
    one, two = Browser(live.url), Browser(live.url)
    one.sign_in("ana@example.com")
    two.sign_in("ana@example.com")
    assert one.post("/account/signout", {"csrf": one.csrf()}).status_code == 303
    assert one.http.get("/account").headers["location"] == "/account/signin"
    assert two.http.get("/account").status_code == 200
    three = Browser(live.url)
    three.sign_in("ana@example.com")
    three.post("/account/signout-everywhere", {"csrf": three.csrf()})
    assert two.http.get("/account").headers["location"] == "/account/signin"


def test_a_session_is_no_bearer_token_and_a_token_no_session(site: Any) -> None:
    live, ana = site
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    session = b.http.cookies[COOKIE]
    r = httpx.post(
        f"{live.url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {session}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 401
    with db(live.db_url) as conn:
        token = accounts.mint_token(conn, ana, "t")
    r = httpx.get(f"{live.url}/account", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 303


def test_export_holds_the_signed_records_and_markdown(site: Any) -> None:
    live, ana = site
    with db(live.db_url) as conn:
        hub = mk_hub(conn, ana, "Lab")
        mk_note(conn, ana, "furnace at 1222 C", hub=hub, title="Sample 14")
        mk_note(conn, ana, "loose thought", title="Loose")
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    r = b.post("/account/export", {"csrf": b.csrf("/account/export")})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "README.txt" in names and "signed/records.jsonl" in names
    assert any(n.startswith("markdown/lab-") and "sample-14" in n for n in names)
    assert any(n.startswith("markdown/notes/loose") for n in names)
    lines = [
        json.loads(x) for x in zf.read("signed/records.jsonl").decode().splitlines()
    ]
    identity = [x for x in lines if x["type"] == "identity"][-1]
    keys = json.loads(json.loads(base64.b64decode(identity["envelope"]))["payload"])[
        "keys"
    ]
    pubs = {k["id"]: fmt.b64u_decode(k["pub"]) for k in keys if k["use"] == "sign"}
    for x in lines:
        if "envelope" in x:
            Ed25519PublicKey.from_public_bytes(pubs[x["key_id"]]).verify(
                base64.b64decode(x["signature"]), base64.b64decode(x["envelope"])
            )
    assert all(
        json.loads(base64.b64decode(x["envelope"]))["author"] == ana.person_id
        for x in lines
        if "envelope" in x
    )


def test_delete_needs_the_handle_and_password_and_erases_everything(
    site: Any, make_account: Any
) -> None:
    live, ana = site
    with db(live.db_url) as conn:
        bea = accounts.create_account(conn, "bea@example.com", PASSWORD, "bea")
        hub = mk_hub(conn, ana, "Ana's hub")
        share(conn, ana, hub, bea, "add")
        mine = mk_note(conn, ana, "mine", hub=hub, title="Mine")
        theirs = mk_note(conn, bea, "bea's", hub=hub, title="Bea's")
        bea_hub = mk_hub(conn, bea, "Bea's hub")
        share(conn, bea, bea_hub, ana, "add")
        in_bea = mk_note(conn, ana, "in bea's hub", hub=bea_hub, title="In Bea's")
    a = Assistant(live)
    a.connect("signin", "ana@example.com")
    b = Browser(live.url)
    b.sign_in("ana@example.com")
    csrf = b.csrf("/account/delete")
    assert (
        b.post(
            "/account/delete", {"csrf": csrf, "handle": "bea", "password": PASSWORD}
        ).status_code
        == 400
    )
    assert (
        b.post(
            "/account/delete",
            {"csrf": csrf, "handle": "ana", "password": "wrong password!"},
        ).status_code
        == 401
    )
    done = b.post(
        "/account/delete", {"csrf": csrf, "handle": "@ana", "password": PASSWORD}
    )
    assert done.status_code == 200 and "deleted" in done.text
    assert a.mcp_status() == 401
    assert b.http.get("/account").headers["location"] == "/account/signin"
    with db(live.db_url) as conn:
        acct = conn.execute(
            "SELECT email, password_hash, status, handle FROM account WHERE id = %s",
            (ana.id,),
        ).fetchone()
        assert (
            acct["email"].endswith("@deleted.invalid") and acct["password_hash"] == "!"
        )
        assert acct["status"] == "disabled" and acct["handle"] == "ana"  # held
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM account_key WHERE account_id = %s AND private_key IS NOT NULL",
                (ana.id,),
            ).fetchone()["n"]
            == 0
        )
        live_left = conn.execute(
            "SELECT count(*) AS n FROM node WHERE author = %s AND tombstoned_at IS NULL",
            (ana.person_id,),
        ).fetchone()["n"]
        assert live_left == 0
        for ref in (mine, in_bea):
            assert conn.execute(
                "SELECT tombstoned_at FROM node WHERE address = %s", (ref,)
            ).fetchone()["tombstoned_at"]
        assert (
            conn.execute(
                "SELECT tombstoned_at FROM node WHERE address = %s", (theirs,)
            ).fetchone()["tombstoned_at"]
            is None
        )
        assert accounts.authenticate(conn, "ana@example.com", PASSWORD) is None
        with pytest.raises(Exception):  # noqa: B017
            accounts.validate_handle("ana") and accounts.create_account(
                conn, "new@example.com", PASSWORD, "ana"
            )
        last = conn.execute(
            "SELECT envelope FROM record WHERE address = %s ORDER BY id DESC LIMIT 1",
            (ana.identity_address,),
        ).fetchone()
        assert (
            json.loads(json.loads(bytes(last["envelope"]))["payload"])["deleted"]
            is True
        )
    a.close()
    _ = graph
