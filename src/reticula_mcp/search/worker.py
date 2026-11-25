"""Background upkeep of the embedding index, inside the server process.

A real embedding model is too slow to load during a tool call and too slow to
run inside a writing transaction. So the server starts one daemon thread
(``hooks.on_server_start``) that

1. loads the model (retrying, with growing waits, when that fails: no network
   for the first download, for example);
2. makes sure the vector index of the current model exists;
3. embeds every node whose vector is missing or stale, a few per transaction,
   then sleeps until a write nudges it (``hooks.on_node_changed``) or a minute
   has passed.

Until step 1 is done ``search`` answers from keywords. Nothing here is needed
for correctness: ``search`` also catches up a few nodes of the viewer's own
world, and ``reticula-mcp reindex`` does everything at once. With the hash
embedder (cheap, embedded inside the write) no thread is started at all.

Several server processes on one database are fine: a transaction-scoped
advisory lock lets one of them catch up at a time.
"""

from __future__ import annotations

import logging
import threading

from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Database, connect
from reticula_mcp.search import index
from reticula_mcp.search.embedder import Embedder, get_embedder

log = logging.getLogger("reticula_mcp.search")

LOCK_KEY = 0x72657465  # one catching-up process per database at a time
WORKER_BATCH = 8  # nodes per transaction
IDLE_SECONDS = 60.0  # the poll when no write nudged the worker
# A write nudges the worker BEFORE its transaction commits, so after a nudge
# the worker looks again after each of these waits.
NUDGE_DELAYS = (0.2, 0.5, 1.0, 2.0, 5.0)
ERROR_SECONDS = 10.0  # wait after a failed pass (database away, ...)
LOAD_RETRY_FIRST = 30.0
LOAD_RETRY_MAX = 900.0


class Worker:
    """The thread described in the module docstring. ``state`` is one of
    ``new``, ``loading``, ``running``, ``stopped``."""

    def __init__(self, db: Database, embedder: Embedder) -> None:
        self._db = db
        self._embedder = embedder
        self._stop = threading.Event()
        self._signal = threading.Event()  # set by a nudge and by stop
        self._thread: threading.Thread | None = None
        self.state = "new"
        self.embedded = 0  # vectors written by this worker (for tests and logs)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="reticula-embedding-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._signal.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def nudge(self) -> None:
        self._signal.set()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ steps

    def _load_model(self) -> bool:
        """Block until the model is loaded. False when stopped first."""
        load = getattr(self._embedder, "load", None)
        delay = LOAD_RETRY_FIRST
        while not self._stop.is_set():
            try:
                if load is not None:
                    load()
                return True
            except Exception:
                log.warning(
                    "embedding model %s did not load; search answers from"
                    " keywords; next try in %.0fs",
                    self._embedder.model,
                    delay,
                    exc_info=True,
                )
            if self._stop.wait(delay):
                return False
            delay = min(delay * 2, LOAD_RETRY_MAX)
        return False

    def _prepare_index(self) -> None:
        try:
            # its own autocommit connection: the index is built CONCURRENTLY
            with connect(self._db.url, autocommit=True) as conn:
                index.ensure_vector_index(conn, self._embedder)
        except Exception:
            log.warning(
                "vector index not created; search scans vectors exactly",
                exc_info=True,
            )

    def _pass(self) -> int:
        """Embed one batch in one transaction. 0 when nothing is left (or
        another process is catching up)."""
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK_KEY,)
            ).fetchone()
            if not row or not row["ok"]:
                return 0
            return index.catch_up(conn, self._embedder, None, limit=WORKER_BATCH)

    def _run(self) -> None:
        self.state = "loading"
        try:
            if not self._load_model():
                return
            self._prepare_index()
            self.state = "running"
            rounds = 0  # short waits left after the last nudge
            while not self._stop.is_set():
                try:
                    done = self._pass()
                except Exception:
                    log.warning("embedding catch-up failed; will retry", exc_info=True)
                    if self._stop.wait(ERROR_SECONDS):
                        return
                    continue
                if done:
                    self.embedded += done
                    continue
                if rounds:
                    delay = NUDGE_DELAYS[len(NUDGE_DELAYS) - rounds]
                    rounds -= 1
                else:
                    delay = IDLE_SECONDS
                if self._signal.wait(delay) and not self._stop.is_set():
                    self._signal.clear()
                    rounds = len(NUDGE_DELAYS)
        finally:
            self.state = "stopped"


_worker: Worker | None = None
_guard = threading.Lock()


def start(settings: Settings, db: Database) -> Worker | None:
    """Start the worker for this process (once). Returns it, or None when the
    configured embedder needs none (the hash embedder) or cannot be built.
    Returns at once and never raises: a server must start without its model."""
    global _worker
    with _guard:
        if _worker is not None and _worker.alive():
            return _worker
        try:
            embedder = get_embedder(settings)
        except Exception:
            log.warning(
                "no embedder (%s); search answers from keywords",
                settings.embedder,
                exc_info=True,
            )
            return None
        if getattr(embedder, "cheap", False):
            return None
        _worker = Worker(db, embedder)
        _worker.start()
        return _worker


def stop(timeout: float = 5.0) -> None:
    """Stop the worker of this process, if any."""
    global _worker
    with _guard:
        worker, _worker = _worker, None
    if worker is not None:
        worker.stop(timeout)


def nudge() -> None:
    """Tell the worker that a node changed. Cheap; nothing happens without one."""
    worker = _worker
    if worker is not None:
        worker.nudge()
