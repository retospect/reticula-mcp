"""/healthz answers from a database ping that is at most HEALTH_CACHE_SECONDS old."""

from __future__ import annotations

import pytest

from reticula_mcp import __version__, server
from reticula_mcp.db.pool import Database
from tests.auth_support import start_live


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Pings:
    def __init__(self) -> None:
        self.calls = 0
        self.result = True


@pytest.fixture
def pings(monkeypatch):
    p = Pings()

    def ping(self: Database) -> bool:
        p.calls += 1
        return p.result

    monkeypatch.setattr(Database, "ping", ping)
    return p


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(server, "_now", c)
    return c


def test_the_window_is_two_seconds():
    assert server.HEALTH_CACHE_SECONDS == 2.0


def test_two_requests_in_the_window_ping_once(fresh_db_url, pings, clock):
    with start_live(fresh_db_url) as live, live.http() as http:
        first = http.get("/healthz")
        clock.now += server.HEALTH_CACHE_SECONDS - 0.1
        second = http.get("/healthz")
        assert pings.calls == 1
        for r in (first, second):
            assert r.status_code == 200
            assert r.json() == {"status": "ok", "version": __version__}


def test_after_the_window_it_pings_again(fresh_db_url, pings, clock):
    with start_live(fresh_db_url) as live, live.http() as http:
        http.get("/healthz")
        clock.now += server.HEALTH_CACHE_SECONDS
        http.get("/healthz")
        assert pings.calls == 2
        http.get("/healthz")
        assert pings.calls == 2


def test_a_failure_is_cached_the_same_way(fresh_db_url, pings, clock):
    pings.result = False
    with start_live(fresh_db_url) as live, live.http() as http:
        first = http.get("/healthz")
        pings.result = True  # the database is back, but the answer is kept
        clock.now += server.HEALTH_CACHE_SECONDS - 0.1
        second = http.get("/healthz")
        assert pings.calls == 1
        for r in (first, second):
            assert r.status_code == 503
            assert r.json() == {"status": "unavailable", "version": __version__}
        clock.now += 0.1
        third = http.get("/healthz")
        assert pings.calls == 2
        assert third.status_code == 200
        assert third.json() == {"status": "ok", "version": __version__}


def test_without_a_clock_change_the_real_answer_is_unchanged(fresh_db_url):
    with start_live(fresh_db_url) as live, live.http() as http:
        r = http.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": __version__}
