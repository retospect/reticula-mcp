"""0.4.2 hardening of registration, sign-in, operator tokens and handles."""

from __future__ import annotations

import io
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from reticula_mcp import accounts, cli
from reticula_mcp.auth import config, passwords, registration, signin
from reticula_mcp.config import Settings
from reticula_mcp.db import connect
from reticula_mcp.errors import ReticulaError
from tests.auth_support import (
    CLAUDE_REDIRECT,
    LOOPBACK_REDIRECT,
    PASSWORD,
    Assistant,
    start_live,
)

# --- A1: who may register ----------------------------------------------------


def _register(live: Any, uri: str) -> int:
    a = Assistant(live, redirect_uri=uri)
    try:
        return a.register().status_code
    finally:
        a.close()


def test_only_loopback_and_known_hosts_register_by_default(fresh_db_url: str) -> None:
    with start_live(fresh_db_url) as live:
        assert _register(live, CLAUDE_REDIRECT) == 201
        assert _register(live, "https://claude.com/api/mcp/auth_callback") == 201
        assert _register(live, "https://app.claude.ai/cb") == 201  # below a known host
        assert _register(live, LOOPBACK_REDIRECT) == 201
        assert _register(live, "https://elsewhere.example/cb") == 400
        assert _register(live, "https://claude.ai.elsewhere.example/cb") == 400
        assert _register(live, "https://notclaude.ai/cb") == 400


def test_the_operator_can_add_a_known_client_host(fresh_db_url: str) -> None:
    """RETICULA_KNOWN_CLIENTS extends who may register, without opening it."""
    hosts = ("claude.ai", "claude.com", "chatgpt.com")
    with start_live(fresh_db_url, known_clients=hosts) as live:
        assert _register(live, "https://chatgpt.com/connector/oauth/cb") == 201
        assert _register(live, "https://elsewhere.example/cb") == 400
    assert Settings.from_env(
        {"RETICULA_KNOWN_CLIENTS": "claude.ai, chatgpt.com"}
    ).known_clients == ("claude.ai", "chatgpt.com")


def test_the_operator_can_open_registration(fresh_db_url: str) -> None:
    with start_live(fresh_db_url, registration="open") as live:
        assert _register(live, "https://elsewhere.example/cb") == 201


def test_a_client_registered_with_another_host_stops_at_authorize(
    fresh_db_url: str,
) -> None:
    """A client stored before the rule (or while registration was open) is
    refused when it asks a person to sign in."""
    with start_live(fresh_db_url) as live:
        client_id = "c_" + secrets.token_urlsafe(18)
        info = OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=[AnyUrl("https://elsewhere.example/cb")],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name="Old client",
            scope=config.SCOPE,
        )
        with connect(fresh_db_url) as conn:
            from reticula_mcp.auth.provider import ReticulaProvider

            ReticulaProvider.store_client(conn, info)
            conn.commit()
        a = Assistant(live, redirect_uri="https://elsewhere.example/cb")
        a.client_id = client_id
        r = a.authorize()
        a.close()
        assert r.status_code != 302 or "/signin" not in r.headers.get("location", "")


def test_the_setting_is_checked() -> None:
    with pytest.raises(ReticulaError):
        Settings(registration="maybe").validate()
    assert Settings.from_env({"RETICULA_REGISTRATION": "open"}).registration == "open"
    assert Settings.from_env({}).registration == "known"
    with pytest.raises(ValueError):
        registration.validate_redirect_uri("https://elsewhere.example/cb", Settings())


# --- A3: a ceiling per e-mail across addresses, and common passwords --------


def test_many_failures_for_one_e_mail_from_many_addresses_slow_it_down(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(signin, "_wait", record)
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        for n in range(config.EMAIL_CEILING + 3):
            conn.execute(
                "INSERT INTO auth_event (kind, subject) VALUES ('signin_email', %s)",
                (f"ana@example.com|10.0.{n}.0/24",),
            )
        conn.commit()
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        a.register()
        page = a.open_page(a.authorize().headers["location"])
        done = a.submit(page, mode="signin", email="ana@example.com")
        a.close()
    assert waited == [8.0]  # 2 ** 3 failures over the ceiling
    assert done.status_code == 303  # no lockout: the right password still signs in


def test_below_the_ceiling_nothing_waits(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(signin, "_wait", record)
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        a.register()
        page = a.open_page(a.authorize().headers["location"])
        assert a.submit(page, mode="signin", email="ana@example.com").status_code == 303
        a.close()
    assert waited == []


def test_a_common_password_is_refused_at_sign_up(fresh_db_url: str) -> None:
    assert passwords.is_common("password1234") or passwords.is_common("1234567890")
    common = next(
        p
        for p in ("1234567890", "password1234", "qwertyuiop")
        if passwords.is_common(p)
    )
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        a.register()
        page = a.open_page(a.authorize().headers["location"], "signup")
        r = a.submit(
            page, mode="signup", email="new@example.com", password=common.upper()
        )
        a.close()
    assert r.status_code == 400
    assert "most common" in r.text


# --- A5: parked requests never opened do not use up the client's cap ---------


def test_unopened_sign_ins_do_not_fill_the_clients_cap(
    fresh_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MAX_PENDING_PER_CLIENT", 3)
    monkeypatch.setattr(config, "MAX_UNOPENED_PER_CLIENT", 50)
    monkeypatch.setattr(config, "MAX_PENDING_PER_ADDRESS", 100)
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        a.register()
        for _ in range(6):  # never opened
            assert a.authorize().status_code == 302
        # a person still gets a page
        page = a.open_page(a.authorize().headers["location"])
        assert page.status_code == 200
        a.close()


# --- A6: operator tokens expire; disable and passwd revoke everything --------


def _cli(monkeypatch: pytest.MonkeyPatch, url: str, *args: str, stdin: str = "") -> int:
    monkeypatch.setenv("RETICULA_DATABASE_URL", url)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(list(args))


def test_an_operator_token_expires_after_90_days_by_default(
    fresh_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
    assert _cli(monkeypatch, fresh_db_url, "token", "--email", "ana@example.com") == 0
    assert (
        _cli(
            monkeypatch,
            fresh_db_url,
            "token",
            "--email",
            "ana@example.com",
            "--no-expiry",
        )
        == 0
    )
    with connect(fresh_db_url) as conn:
        rows = conn.execute(
            "SELECT expires_at FROM oauth_token WHERE kind = 'access' ORDER BY id"
        ).fetchall()
    first, second = rows[0]["expires_at"], rows[1]["expires_at"]
    assert first is not None
    assert abs(first - (datetime.now(UTC) + timedelta(days=90))) < timedelta(minutes=5)
    assert second is None


def test_disable_revokes_every_connection_and_token(
    fresh_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        tok = accounts.mint_token(conn, ana, "laptop")
        conn.commit()
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        a.connect("signin", "ana@example.com")
        assert a.mcp_status() == 200 and a.mcp_status(tok) == 200
        assert (
            _cli(
                monkeypatch,
                fresh_db_url,
                "account",
                "disable",
                "--email",
                "ana@example.com",
            )
            == 0
        )
        assert a.mcp_status() == 401 and a.mcp_status(tok) == 401
        assert a.refresh().status_code != 200
        a.close()
    with connect(fresh_db_url) as conn:
        assert accounts.authenticate(conn, "ana@example.com", PASSWORD) is None
        left = conn.execute(
            "SELECT count(*) AS n FROM oauth_token WHERE account_id = %s AND revoked_at IS NULL",
            (ana.id,),
        ).fetchone()["n"]
    assert left == 0


def test_passwd_sets_a_new_password_and_revokes_everything(
    fresh_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with connect(fresh_db_url) as conn:
        ana = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        tok = accounts.mint_token(conn, ana, "laptop")
        conn.commit()
    new = "a much longer new passphrase"
    with start_live(fresh_db_url) as live:
        a = Assistant(live)
        a.connect("signin", "ana@example.com")
        assert (
            _cli(
                monkeypatch,
                fresh_db_url,
                "account",
                "passwd",
                "--email",
                "ana@example.com",
                stdin=new + "\n",
            )
            == 0
        )
        assert a.mcp_status() == 401 and a.mcp_status(tok) == 401
        a.close()
    with connect(fresh_db_url) as conn:
        assert accounts.authenticate(conn, "ana@example.com", PASSWORD) is None
        assert accounts.authenticate(conn, "ana@example.com", new) is not None
    common = next(
        p
        for p in ("1234567890", "password1234", "qwertyuiop")
        if passwords.is_common(p)
    )
    assert (
        _cli(
            monkeypatch,
            fresh_db_url,
            "account",
            "passwd",
            "--email",
            "ana@example.com",
            stdin=common + "\n",
        )
        == 1
    )


# --- C12: handles that could pass for the instance or an administrator -------


@pytest.mark.parametrize(
    "handle", ["reticula-help", "the-admin", "admins", "myreticula"]
)
def test_a_handle_containing_reticula_or_admin_is_refused(handle: str) -> None:
    with pytest.raises(ReticulaError) as err:
        accounts.validate_handle(handle)
    assert "reserved" in err.value.message


def test_an_e_mail_starting_with_admin_gets_another_handle(conn: Any) -> None:
    acct = accounts.create_account(conn, "admin@example.com", PASSWORD)
    assert "admin" not in acct.handle and "reticula" not in acct.handle
    system = accounts.ensure_system_account(conn)
    assert system.handle == "reticula"
    _ = time  # keep the import used across platforms
