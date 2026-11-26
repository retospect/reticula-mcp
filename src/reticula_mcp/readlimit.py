"""Limits on the tools that only read, per account, in this process: how many
run at once, how many start in a window of time, and how long one may take.

They keep one account from holding the connection pool. They are counted in
memory, not in the database: a row per read would make every search a write.
A server with several worker processes has them per process.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from reticula_mcp.config import parse_rate


class ReadRefused(Exception):
    """A read is refused. ``wait``: seconds until the window has room again,
    or None when too many reads of the account run at once."""

    def __init__(self, wait: int | None) -> None:
        super().__init__("read refused")
        self.wait = wait


@dataclass
class _Account:
    running: threading.BoundedSemaphore
    started: deque[float] = field(default_factory=deque)


class ReadLimits:
    """``rate``: reads that may start per window ("300/minute"); a count of 0
    switches the limit off. ``concurrency``: reads that may run at once
    (0: no limit). ``wait``: seconds a read waits for a free place."""

    def __init__(
        self,
        rate: str,
        concurrency: int,
        *,
        wait: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit, self.window = parse_rate(rate)
        self.concurrency = concurrency
        self.wait = wait
        self._clock = clock
        self._lock = threading.Lock()
        self._accounts: dict[str, _Account] = {}

    def _account(self, key: str) -> _Account:
        with self._lock:
            found = self._accounts.get(key)
            if found is None:
                found = _Account(threading.BoundedSemaphore(max(self.concurrency, 1)))
                self._accounts[key] = found
            return found

    def _count(self, account: _Account) -> None:
        """Count one start in the window, or refuse with the seconds to wait."""
        if self.limit <= 0:
            return
        now = self._clock()
        with self._lock:
            started = account.started
            while started and started[0] <= now - self.window:
                started.popleft()
            if len(started) >= self.limit:
                raise ReadRefused(max(1, int(started[0] + self.window - now + 0.999)))
            started.append(now)

    @contextmanager
    def read(self, key: str) -> Iterator[None]:
        """Around one read of the account ``key``. Counted when it starts;
        waits up to ``wait`` seconds for a place among the ``concurrency``
        reads of the account (holding nothing else meanwhile), then refuses."""
        account = self._account(key)
        self._count(account)
        if self.concurrency <= 0:
            yield
            return
        if not account.running.acquire(timeout=self.wait):
            raise ReadRefused(None)
        try:
            yield
        finally:
            account.running.release()


class Deadline:
    """What :func:`deadline` yields: ``fired`` once the cancel was sent."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.fired = False


@contextmanager
def deadline(conn: Any, seconds: float) -> Iterator[Deadline]:
    """Cancel what ``conn`` runs once ``seconds`` have passed: the statement
    then ends with QueryCanceled. A connection whose cancel was sent is closed
    at the end, so that the pool never hands it on with a cancel that could
    still arrive for the next call. ``seconds`` of 0 or less: no deadline."""
    state = Deadline(seconds)
    if seconds <= 0:
        yield state
        return
    lock = threading.Lock()
    done = False

    def fire() -> None:
        with lock:
            if done:
                return
            state.fired = True
        conn.cancel_safe()

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        yield state
    finally:
        with lock:
            done = True
        timer.cancel()
        if state.fired:
            conn.close()
