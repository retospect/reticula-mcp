"""Command-line behaviour that needs no server."""

from __future__ import annotations

import logging

from reticula_mcp import cli


def _access_record(path: str) -> logging.LogRecord:
    # The five arguments uvicorn gives an access record.
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", path, "1.1", 200),
        None,
    )


def test_access_log_lines_carry_no_query_string():
    record = _access_record("/signin?req=secret-handle&mode=signup")
    assert cli._NoQueryString().filter(record) is True
    assert record.args[2] == "/signin"
    assert "secret-handle" not in record.getMessage()


def test_access_log_filter_leaves_other_records_alone():
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "%s?%s", ("a", "b"), None
    )
    assert cli._NoQueryString().filter(record) is True
    assert record.getMessage() == "a?b"


def test_an_unreachable_database_is_one_line(monkeypatch, capsys):
    monkeypatch.setenv("RETICULA_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/x")
    assert cli.main(["migrate"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("[error:Unavailable] cannot reach the database")
    assert "Traceback" not in err


def test_a_public_url_must_be_https_or_this_machine(monkeypatch, capsys):
    monkeypatch.setenv("RETICULA_DATABASE_URL", "postgresql://x@127.0.0.1:1/x")
    monkeypatch.setenv("RETICULA_PUBLIC_URL", "http://notes.example.net")
    assert cli.main(["migrate"]) == 1
    assert "RETICULA_PUBLIC_URL" in capsys.readouterr().err
    monkeypatch.setenv("RETICULA_PUBLIC_URL", "https://notes.example.net/")
    monkeypatch.setenv("RETICULA_DATABASE_URL", "")
    assert (
        cli.main(["migrate"]) == 1
    )  # past the URL check, stopped by the missing database
    assert "RETICULA_DATABASE_URL" in capsys.readouterr().err
