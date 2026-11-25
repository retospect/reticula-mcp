"""The request line: who called, with what, and why the server refused; and
nothing of what the request carried."""

from __future__ import annotations

import logging

import pytest
from starlette.datastructures import Headers

from reticula_mcp import accounts
from reticula_mcp.db import connect
from reticula_mcp.requestlog import one_line, reason_of
from tests.auth_support import PASSWORD, Assistant, start_live

JSON = Headers({"content-type": "application/json"})


def test_the_reason_is_the_servers_own_error_text():
    protocol = b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Unsupported protocol version: 2099-01-01"}}'
    assert reason_of(400, JSON, protocol) == (
        "Bad Request: Unsupported protocol version: 2099-01-01"
    )
    signin = b'{"error":"invalid_grant","error_description":"authorization code is not valid"}'
    assert reason_of(400, JSON, signin) == (
        "invalid_grant: authorization code is not valid"
    )
    challenge = Headers(
        {"www-authenticate": 'Bearer error="invalid_token", error_description="x"'}
    )
    assert reason_of(401, challenge, b"") == "invalid_token"
    assert reason_of(401, Headers(), b"") == "no token"
    assert reason_of(404, Headers({"content-type": "text/plain"}), b"Not Found") == ""
    assert reason_of(400, JSON, b"not json") == ""


def test_the_reason_stops_before_what_a_validation_message_quotes():
    body = (
        b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32602,"message":'
        b'"Validation error: 1 validation error for JSONRPCMessage\\n  Field required'
        b" [type=missing, input_value={'text': 'the secret of the note'}, input_type=dict]\"}}"
    )
    said = reason_of(400, JSON, body)
    assert said.startswith("Validation error: 1 validation error")
    assert "secret" not in said and "input_value" not in said


def test_a_line_cannot_be_broken_or_grown_by_what_a_caller_sends():
    assert one_line("Probe/1.0\r\n2026-01-01 GET /forged 200", 80) == (
        "Probe/1.0 2026-01-01 GET /forged 200"
    )
    assert one_line("Pröbe\x00/1.0", 80) == "Pr be /1.0"
    assert len(one_line("x" * 500, 80)) == 80


@pytest.fixture
def lines(fresh_db_url, caplog):
    caplog.set_level(logging.INFO, logger="reticula_mcp.request")
    with start_live(fresh_db_url, trusted_proxies=("127.0.0.1",)) as live:

        def read() -> list[str]:
            return [
                r.getMessage()
                for r in caplog.records
                if r.name == "reticula_mcp.request"
            ]

        yield live, read


def test_a_refused_request_is_logged_with_caller_program_and_reason(lines):
    live, read = lines
    http = live.http()
    assert http.get("/healthz").status_code == 200
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"User-Agent": "Probe/1.0", "X-Forwarded-For": "198.51.100.7"},
    )
    assert r.status_code == 401
    assert http.get("/signin?req=secret-handle&mode=signup").status_code in (200, 400)
    got = read()
    assert not any("/healthz" in line for line in got)
    (refused,) = [line for line in got if line.startswith("POST /mcp ")]
    assert refused.startswith("POST /mcp 401 ")
    assert " from=198.51.100.7 " in refused
    assert ' agent="Probe/1.0"' in refused
    assert ' reason="' in refused
    (page,) = [line for line in got if line.startswith("GET /signin ")]
    assert "secret-handle" not in page and "?" not in page


def test_a_signed_in_request_names_its_connection_and_a_bad_one_says_why(lines):
    live, read = lines
    with connect(live.db_url) as conn:
        accounts.create_account(conn, "log@example.com", PASSWORD)
        conn.commit()
    assistant = Assistant(live)
    try:
        tokens = assistant.connect("signin", "log@example.com")
        assert assistant.mcp_status() == 200
        r = assistant.api.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {tokens['access_token']}",
                "MCP-Protocol-Version": "2099-01-01",
            },
        )
        assert r.status_code == 400
    finally:
        assistant.close()
    got = read()
    served = [line for line in got if line.startswith("POST /mcp 200 ")]
    assert served and f" client={assistant.client_id[:10]}" in served[-1]
    (bad,) = [line for line in got if line.startswith("POST /mcp 400 ")]
    assert " mcp=2099-01-01 " in bad
    assert 'reason="Bad Request: Unsupported protocol version' in bad
    # nothing a request carried: no token, no password, no code
    everything = "\n".join(got)
    for secret in (tokens["access_token"], tokens["refresh_token"], PASSWORD):
        assert secret not in everything
    assert "code=" not in everything and "state=" not in everything
