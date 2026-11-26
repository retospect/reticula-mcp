"""Shipped instruction texts and the loader that seeds them into an instance.

The texts are markdown files in ``content/instructions/`` (shipped in the
wheel). The first line of each file is ``# Title``; the rest is the note text;
the file name without ``.md`` is the slug. The slug is stored in the atom's tags
as ``slug:<name>``, which is how the loader finds an atom it wrote earlier.

:func:`ensure_shipped_content` is written against the small :class:`GraphWriter`
protocol, whose method names and parameters mirror ``graph/api.py`` (AGENTS.md,
seam 10). The loader never imports the graph package; the hooks pass it the
``reticula_mcp.graph.api`` module.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Protocol

from reticula_mcp import accounts
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn

SLUG_PREFIX = "slug:"
INSTRUCTIONS_HUB_SLUG = "hub-instructions"
DEBUG_HUB_SLUG = "hub-debug"
STARTER_SLUG = "starter"
SHIPPED_TAG = "shipped"

INSTRUCTIONS_HUB_TITLE = "Reticula instructions"
INSTRUCTIONS_HUB_TEXT = (
    "Instructions that ship with this Reticula instance, written by the instance"
    " itself. Every account can read this hub. Start with the starting instruction."
)
DEBUG_HUB_TITLE = "Debug notes"
DEBUG_HUB_TEXT = (
    "Problem reports from assistants. Any account can add a note here; only the"
    " instance operator reads them."
)


class GraphWriter(Protocol):
    """The four graph write operations the loader and hooks need.

    Names and parameters are the ones in ``graph/api.py``; the real binding is
    that module itself.
    """

    def create_atom(
        self,
        conn: Conn,
        author: Account,
        *,
        kind: str,
        text: str,
        title: str | None = None,
        tags: Sequence[str] | None = None,
        hub_node_id: int | None = None,
        private: bool = False,
        supersedes: Sequence[str] = (),
        ext_id: str | None = None,
        anchor: str | None = None,
        model: str | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def update_atom(
        self,
        conn: Conn,
        author: Account,
        node_id: int,
        *,
        text: str | None = None,
        title: str | None = None,
        tags: Sequence[str] | None = None,
        private: bool | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def add_membership(
        self, conn: Conn, actor: Account, node_id: int, hub_node_id: int
    ) -> None: ...

    def set_grant(
        self,
        conn: Conn,
        actor: Account,
        hub_node_id: int,
        grantee_person_id: str,
        mode: str,
    ) -> None: ...


@dataclass(frozen=True)
class Instruction:
    """One shipped instruction text."""

    slug: str
    title: str
    text: str


@dataclass
class BootstrapResult:
    """What one run of the loader did (slugs, in the order they were handled)."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)  # person IDs given read access


def parse_instruction(slug: str, source: str) -> Instruction:
    """Split ``# Title`` from the body. A file without a title uses its slug."""
    lines = source.strip("\n").split("\n")
    title = slug
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    body = "\n".join(lines).strip("\n")
    return Instruction(slug=slug, title=title, text=body + "\n")


def shipped_instructions() -> list[Instruction]:
    """The instruction files inside the package, sorted by slug."""
    folder = resources.files("reticula_mcp.content").joinpath("instructions")
    found = [
        parse_instruction(item.name[: -len(".md")], item.read_text(encoding="utf-8"))
        for item in folder.iterdir()
        if item.name.endswith(".md")
    ]
    return sorted(found, key=lambda i: i.slug)


def starter_instruction() -> Instruction:
    for item in shipped_instructions():
        if item.slug == STARTER_SLUG:
            return item
    raise RuntimeError("the starter instruction is missing from the package")


def slug_tag(slug: str) -> str:
    return f"{SLUG_PREFIX}{slug}"


def system_account(conn: Conn) -> Account | None:
    """The instance's system account if it exists (never creates it)."""
    row = conn.execute("SELECT id FROM account WHERE status = 'system'").fetchone()
    return accounts.get_account(conn, row["id"]) if row else None


def find_shipped_node(conn: Conn, system: Account, slug: str) -> dict[str, Any] | None:
    """The live node the system account wrote for ``slug``, or None."""
    return conn.execute(
        "SELECT * FROM node WHERE author = %s AND %s = ANY(tags)"
        " AND tombstoned_at IS NULL ORDER BY id LIMIT 1",
        (system.person_id, slug_tag(slug)),
    ).fetchone()


def _ensure_atom(
    conn: Conn,
    api: GraphWriter,
    system: Account,
    result: BootstrapResult,
    *,
    slug: str,
    kind: str,
    title: str,
    text: str,
    hub_node_id: int | None = None,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the atom for ``slug``, or publish a new version if text or title
    differ. Returns the node row (as the graph API returned it)."""
    existing = find_shipped_node(conn, system, slug)
    if existing is None:
        node = api.create_atom(
            conn,
            system,
            kind=kind,
            text=text,
            title=title,
            tags=[slug_tag(slug), SHIPPED_TAG],
            hub_node_id=hub_node_id,
            payload_extra=payload_extra,
        )
        result.created.append(slug)
        return node
    if existing["text"] != text or existing["title"] != title:
        node = api.update_atom(conn, system, existing["id"], text=text, title=title)
        result.updated.append(slug)
        return node
    result.unchanged.append(slug)
    return existing


def _has_grant(conn: Conn, hub_node_id: int, person_id: str) -> bool:
    """Any grant row, including ``none``: a revoked reader stays revoked."""
    row = conn.execute(
        "SELECT 1 FROM hub_grant WHERE hub_node_id = %s AND grantee = %s",
        (hub_node_id, person_id),
    ).fetchone()
    return row is not None


def ensure_shipped_content(
    conn: Conn,
    api: GraphWriter,
    *,
    instructions: Iterable[Instruction] | None = None,
) -> BootstrapResult:
    """Make the instance's shipped content exist. Idempotent.

    * the instructions hub and the debug hub, owned by the system account
      (the debug hub carries ``"accept": "anyone"`` and is granted to nobody);
    * one ``instruction`` atom per shipped file, filed under the instructions
      hub, with a new version only when the file's text or title changed;
    * read access on the instructions hub for every existing account.

    ``instructions`` replaces the packaged files (tests).
    """
    system = accounts.ensure_system_account(conn)
    result = BootstrapResult()
    hub = _ensure_atom(
        conn,
        api,
        system,
        result,
        slug=INSTRUCTIONS_HUB_SLUG,
        kind="hub",
        title=INSTRUCTIONS_HUB_TITLE,
        text=INSTRUCTIONS_HUB_TEXT,
        payload_extra={"preset": "personal"},
    )
    _ensure_atom(
        conn,
        api,
        system,
        result,
        slug=DEBUG_HUB_SLUG,
        kind="hub",
        title=DEBUG_HUB_TITLE,
        text=DEBUG_HUB_TEXT,
        payload_extra={"accept": "anyone"},
    )
    for item in shipped_instructions() if instructions is None else instructions:
        _ensure_atom(
            conn,
            api,
            system,
            result,
            slug=item.slug,
            kind="instruction",
            title=item.title,
            text=item.text,
            hub_node_id=hub["id"],
        )
    rows = conn.execute(
        "SELECT person_id FROM account WHERE status IN ('active', 'provisional')"
        " ORDER BY id"
    ).fetchall()
    for row in rows:
        if not _has_grant(conn, hub["id"], row["person_id"]) or _left_it(
            conn, hub["id"], row["person_id"]
        ):
            api.set_grant(conn, system, hub["id"], row["person_id"], "read")
            result.granted.append(row["person_id"])
    return result


def _left_it(conn: Conn, hub_node_id: int, person_id: str) -> bool:
    """The account itself set its grant on this hub to ``none``. The
    instructions hub can no longer be left (graph.handlers.share), so a row
    written before that rule is given back as ``read``; a ``none`` written by
    anyone else stays."""
    row = conn.execute(
        "SELECT 1 FROM hub_grant WHERE hub_node_id = %s AND grantee = %s"
        " AND mode = 'none' AND granted_by = grantee",
        (hub_node_id, person_id),
    ).fetchone()
    return row is not None
