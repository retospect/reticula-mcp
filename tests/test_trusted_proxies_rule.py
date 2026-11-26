"""RETICULA_TRUSTED_PROXIES: an https public URL needs an address or ``none``."""

from __future__ import annotations

import logging

import pytest

from reticula_mcp import cli
from reticula_mcp.auth import build_auth
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database
from reticula_mcp.errors import ReticulaError

URL = "https://notes.example.net"


def _env(**values: str) -> dict[str, str]:
    return {f"RETICULA_{k}": v for k, v in values.items()}


@pytest.fixture
def cli_env(monkeypatch):
    """A database nobody listens on: a command that gets past the proxy rule
    stops with 'cannot reach the database'."""
    for name in ("PUBLIC_URL", "TRUSTED_PROXIES", "HOST", "PORT"):
        monkeypatch.delenv(f"RETICULA_{name}", raising=False)
    monkeypatch.setenv("RETICULA_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/x")
    return monkeypatch


# parsing -------------------------------------------------------------------


@pytest.mark.parametrize("word", ["none", "None", "NONE", " nOnE "])
def test_none_alone_trusts_nobody_on_purpose(word):
    s = Settings.from_env(_env(PUBLIC_URL=URL, TRUSTED_PROXIES=word))
    assert s.trusted_proxies == ()
    assert s.trusted_proxies_none is True


def test_empty_and_addresses_do_not_say_none():
    assert Settings.from_env(_env(PUBLIC_URL=URL)).trusted_proxies_none is False
    s = Settings.from_env(_env(TRUSTED_PROXIES="127.0.0.1, 10.0.0.0/8"))
    assert s.trusted_proxies == ("127.0.0.1", "10.0.0.0/8")
    assert s.trusted_proxies_none is False


@pytest.mark.parametrize(
    "value", ["none,127.0.0.1", "127.0.0.1,none", "10.0.0.0/8, NONE", "none,none"]
)
def test_none_beside_other_entries_is_a_configuration_error(value):
    with pytest.raises(ReticulaError) as err:
        Settings.from_env(_env(TRUSTED_PROXIES=value))
    assert "none must stand alone" in err.value.message


def test_none_and_addresses_set_together_in_code_is_an_error():
    with pytest.raises(ReticulaError, match="none must stand alone"):
        Settings(trusted_proxies=("127.0.0.1",), trusted_proxies_none=True).validate()


# the rule at start ---------------------------------------------------------


def test_serving_with_an_https_url_and_no_proxies_is_refused(cli_env, capsys):
    cli_env.setenv("RETICULA_PUBLIC_URL", URL)
    assert cli.main(["serve"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("[error:BadInput] ")
    assert URL in err
    assert "RETICULA_TRUSTED_PROXIES is empty" in err
    assert "172.31.88.1" in err
    assert "\nhint: " in err
    assert "RETICULA_TRUSTED_PROXIES=none" in err
    assert "one rate-limit bucket" in err
    assert "cannot reach the database" not in err  # refused before any connection


def test_serve_refuses_before_it_migrates(cli_env, capsys):
    cli_env.setenv("RETICULA_PUBLIC_URL", URL)
    assert cli.main(["serve", "--migrate"]) == 1
    err = capsys.readouterr().err
    assert "RETICULA_TRUSTED_PROXIES is empty" in err
    assert "cannot reach the database" not in err


@pytest.mark.parametrize(
    "public_url, proxies",
    [
        (URL, "none"),
        (URL, "NONE"),
        (URL, "172.31.88.1"),
        ("http://127.0.0.1:8787", ""),
        ("http://localhost:8787", ""),
        ("", ""),
    ],
)
def test_serve_is_not_refused_when_it_is_decided_or_local(
    cli_env, capsys, public_url, proxies
):
    cli_env.setenv("RETICULA_PUBLIC_URL", public_url)
    cli_env.setenv("RETICULA_TRUSTED_PROXIES", proxies)
    assert cli.main(["serve"]) == 1  # on to the unreachable database
    err = capsys.readouterr().err
    assert "cannot reach the database" in err
    assert "RETICULA_TRUSTED_PROXIES" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["migrate"],
        ["token", "--email", "a@example.com", "--label", "x"],
        ["account", "create", "--email", "a@example.com", "--password", "pw"],
    ],
)
def test_other_commands_are_not_refused_for_it(cli_env, capsys, argv):
    cli_env.setenv("RETICULA_PUBLIC_URL", URL)
    assert cli.main(argv) == 1  # on to the unreachable database
    err = capsys.readouterr().err
    assert "cannot reach the database" in err
    assert "RETICULA_TRUSTED_PROXIES" not in err


# the warning ---------------------------------------------------------------


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_none_starts_with_one_short_warning(caplog):
    db = Database("postgresql://nowhere/none")  # never opened
    settings = Settings(public_url=URL, trusted_proxies_none=True)
    with caplog.at_level(logging.WARNING, logger="reticula_mcp.auth"):
        build_auth(settings, db)
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert "none" in msgs[0] and "one rate-limit bucket" in msgs[0]
    assert len(msgs[0]) < 120


def test_none_on_a_local_url_says_nothing(caplog):
    db = Database("postgresql://nowhere/none")
    with caplog.at_level(logging.WARNING, logger="reticula_mcp.auth"):
        build_auth(Settings(trusted_proxies_none=True), db)
    assert not _warnings(caplog)
