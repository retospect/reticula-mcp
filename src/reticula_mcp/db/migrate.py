"""Forward-only SQL migration runner.

* Files: ``db/migrations/NNNN_name.sql`` (4+ digits, lowercase snake name).
* Applied in numeric order; gaps are fine; any file not in the ledger is
  applied, even when a higher number is already recorded (lanes merge in any
  order). Duplicate numbers are an error.
* One transaction per file. Ledger: ``schema_migration(version, name,
  checksum, applied_at)``. A sealed file whose bytes changed is refused
  (``checksum`` mismatch): add a new migration instead.
* A ledger row without a file means a newer version has used the database.
  That is refused unless ``allow_newer`` is set: this version does not know
  what the newer schema changed.
* A session advisory lock serialises concurrent runners.
* Idempotent: running twice applies nothing the second time.

Number ranges: foundation 0001-0099, sign-in 0100-0199, graph 0200-0299,
search 0300-0399, content 0400-0499, integration 0900+.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import psycopg

_NAME_RE = re.compile(r"^(\d{4,})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x72657469  # 'reti'

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def migrations_dir() -> Path:
    return Path(str(resources.files("reticula_mcp.db") / "migrations"))


def discover(directory: Path | None = None) -> list[Migration]:
    """All migration files, sorted by version."""
    found: dict[int, Migration] = {}
    for path in (directory or migrations_dir()).glob("*.sql"):
        match = _NAME_RE.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name: {path.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration number {version}: {found[version].path.name}"
                f" and {path.name}"
            )
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        found[version] = Migration(version, match.group(2), path, checksum)
    return [found[v] for v in sorted(found)]


def applied(conn: psycopg.Connection[Any]) -> dict[int, str]:
    """``{version: checksum}`` from the ledger ({} when it does not exist)."""
    exists = conn.execute("SELECT to_regclass('schema_migration')").fetchone()
    if not exists or _first(exists) is None:
        conn.rollback()
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migration").fetchall()
    conn.rollback()
    return {int(str(_col(r, "version", 0))): str(_col(r, "checksum", 1)) for r in rows}


def unknown(conn: psycopg.Connection[Any], directory: Path | None = None) -> list[str]:
    """Applied migrations this version has no file for, as ``NNNN_name``: a
    newer version has used the database."""
    known = {m.version for m in discover(directory)}
    exists = conn.execute("SELECT to_regclass('schema_migration')").fetchone()
    if not exists or _first(exists) is None:
        conn.rollback()
        return []
    rows = conn.execute(
        "SELECT version, name FROM schema_migration ORDER BY version"
    ).fetchall()
    conn.rollback()
    found = [(int(str(_col(r, "version", 0))), str(_col(r, "name", 1))) for r in rows]
    return [f"{version:04d}_{name}" for version, name in found if version not in known]


def newer_schema_message(names: list[str]) -> str:
    more = f" and {len(names) - 3} more" if len(names) > 3 else ""
    return (
        "the database has migrations this version does not have"
        f" ({', '.join(names[:3])}{more}): a newer version has used it"
    )


def pending(
    conn: psycopg.Connection[Any], directory: Path | None = None
) -> list[Migration]:
    done = applied(conn)
    return [m for m in discover(directory) if m.version not in done]


def migrate(
    conn: psycopg.Connection[Any],
    directory: Path | None = None,
    *,
    allow_newer: bool = False,
) -> list[Migration]:
    """Apply every pending migration; return the ones applied.

    ``conn`` must be idle (no open transaction); this function commits.
    A database a newer version has used is refused before anything is applied,
    unless ``allow_newer`` is set.
    """
    files = discover(directory)
    ran: list[Migration] = []
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    try:
        conn.execute(LEDGER_DDL)
        conn.commit()
        done = applied(conn)
        newer = unknown(conn, directory)
        if newer and not allow_newer:
            raise MigrationError(newer_schema_message(newer))
        for m in files:
            if m.version in done:
                if done[m.version] != m.checksum:
                    raise MigrationError(
                        f"migration {m.path.name} changed after it was applied;"
                        " add a new migration instead of editing a sealed one"
                    )
                continue
            try:
                with conn.transaction():
                    conn.execute(m.sql)
                    conn.execute(
                        "INSERT INTO schema_migration (version, name, checksum)"
                        " VALUES (%s, %s, %s)",
                        (m.version, m.name, m.checksum),
                    )
            except psycopg.Error as exc:
                raise MigrationError(f"{m.path.name} failed: {exc}") from exc
            ran.append(m)
        conn.commit()
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        conn.commit()
    return ran


def _first(row: Any) -> object:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _col(row: Any, name: str, index: int) -> object:
    return row[name] if isinstance(row, dict) else row[index]
