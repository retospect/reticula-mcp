"""Debug notes for the operator.

Assistants write ``kind: debug`` notes into the instance's debug hub (owned by
the system account, ``"accept": "anyone"``, granted to nobody). Only the
operator reads them, through ``reticula-mcp debug-notes``, which calls
:func:`list_debug_notes` with direct database access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from reticula_mcp import records
from reticula_mcp.content import bootstrap
from reticula_mcp.db.pool import Conn


@dataclass(frozen=True)
class DebugNote:
    address: str
    created_at: datetime
    author_handle: str | None
    author_person_id: str
    model: str | None
    by: str | None  # "person" or "agent", as the writer declared it
    title: str | None
    text: str

    @property
    def short_id(self) -> str:
        return self.address[-8:]


def debug_hub(conn: Conn) -> dict[str, Any] | None:
    """The debug hub's node row, or None before the first ``migrate``."""
    system = bootstrap.system_account(conn)
    if system is None:
        return None
    return bootstrap.find_shipped_node(conn, system, bootstrap.DEBUG_HUB_SLUG)


def list_debug_notes(conn: Conn, limit: int = 20) -> list[DebugNote]:
    """Recent debug notes filed in the debug hub, newest first.

    Notes marked private are left out (they are readable by their author only).
    The model and the writer's own declaration (``origin``) come from the signed
    record, not from a materialised copy.
    """
    hub = debug_hub(conn)
    if hub is None:
        return []
    rows = conn.execute(
        "SELECT n.address, n.title, n.text, n.created_at, n.author,"
        " n.head_record_id, a.handle AS author_handle"
        " FROM node n"
        " JOIN membership m ON m.node_id = n.id AND NOT m.withdrawn"
        " LEFT JOIN account a ON a.person_id = n.author"
        " WHERE m.hub_node_id = %s AND n.kind = 'debug'"
        " AND n.tombstoned_at IS NULL AND n.audience <> 'private'"
        " ORDER BY n.created_at DESC, n.id DESC LIMIT %s",
        (hub["id"], max(1, min(limit, 500))),
    ).fetchall()
    notes = []
    for row in rows:
        origin: dict[str, Any] = {}
        record = records.get_record(conn, row["head_record_id"])
        if record is not None and not record.blanked:
            found = record.payload.get("origin")
            if isinstance(found, dict):
                origin = found
        notes.append(
            DebugNote(
                address=row["address"],
                created_at=row["created_at"],
                author_handle=row["author_handle"],
                author_person_id=row["author"],
                model=origin.get("model"),
                by=origin.get("by"),
                title=row["title"],
                text=row["text"],
            )
        )
    return notes


def format_debug_notes(notes: list[DebugNote]) -> str:
    """Plain text for the terminal."""
    if not notes:
        return "no debug notes"
    blocks = []
    for note in notes:
        who = f"@{note.author_handle}" if note.author_handle else note.author_person_id
        model = note.model or "unknown model"
        by = f" ({note.by})" if note.by else ""
        head = (
            f"{note.created_at:%Y-%m-%d %H:%M} UTC  {who}{by}  model: {model}"
            f"  id: {note.short_id}"
        )
        title = f"\n{note.title}" if note.title else ""
        body = "\n".join("  " + line for line in note.text.rstrip().split("\n"))
        blocks.append(f"{head}{title}\n{body}")
    return "\n\n".join(blocks)
