"""Settings: what a person or a connection switched, through one ref.

``edit(ref="settings", text="word handles: on")`` changes a setting; profile's
reply lists them all with their values and the call that changes each. No
tool gains a parameter for this: a new parameter, read before an assistant's
first call, cost the gate in the trials (word handles, round fifteen).

Each setting has a scope:

- ``connection``: the connection the call came through (connection.py), in
  ``connection_setting``. Word handles are per connection: the voice app on
  the phone may want them, the laptop not.
- ``account``: the account, in ``account_setting`` (migration 0352). Whether
  the account is listed in the directory (RETICULA3) is per account.

"settings" is never a note's ref: an address starts with ``ret:``, an id is
hex, a handle is two to four words of the handle lists, and "settings" is
none of these (``is_settings_ref``).
"""

from __future__ import annotations

from dataclasses import dataclass

from reticula_mcp import connection
from reticula_mcp.accounts import Account
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, ReticulaError

REF = "settings"


@dataclass(frozen=True)
class Setting:
    name: str  # as written: "word handles"
    key: str  # as stored: "word_handles"
    scope: str  # connection | account
    values: tuple[str, ...]  # in the order the choices are written
    says: dict[str, str]  # what each value does, for profile's reply
    default: str | None = None  # when never set; None: the first value

    @property
    def unset(self) -> str:
        return self.default or self.values[0]


SETTINGS = (
    Setting(
        "word handles",
        "word_handles",
        "connection",
        ("off", "on"),
        {
            "off": "a note is named by its id",
            "on": "a note's id at the start of a line is followed by words to say aloud",
        },
    ),
    Setting(
        "listed",
        "listed",
        "account",
        ("yes", "no"),
        {
            "yes": 'search(kind="person") shows this account\'s handle to other'
            " people on this instance",
            "no": 'search(kind="person") leaves this account out; others can still'
            " share with it by its exact handle",
        },
        # a new account is unlisted; 0304 kept every older account listed
        default="no",
    ),
)
_BY_NAME = {s.name: s for s in SETTINGS}


def is_settings_ref(ref: str) -> bool:
    return ref.strip().lower() == REF


def value(conn: Conn, viewer: Account, name: str) -> str:
    """The value of setting ``name`` for this account, or this call's
    connection: its default when it was never set, or when a per-connection
    setting is asked outside a tool call."""
    setting = _BY_NAME[name]
    if setting.scope == "account":
        row = conn.execute(
            "SELECT value FROM account_setting WHERE account_id = %s AND name = %s",
            (viewer.id, setting.key),
        ).fetchone()
    else:
        key = connection.current()
        row = (
            conn.execute(
                "SELECT value FROM connection_setting"
                " WHERE connection = %s AND name = %s",
                (key, setting.key),
            ).fetchone()
            if key
            else None
        )
    return str(row["value"]) if row else setting.unset


def listed(conn: Conn, viewer: Account) -> bool:
    """Whether ``viewer`` is listed in the directory (RETICULA3 calls this)."""
    return value(conn, viewer, "listed") == "yes"


def unlisted_sql(account_id_column: str) -> str:
    """A SQL condition, true for an account that is not listed (it never set
    ``listed: yes``; unlisted is the default), for a query that lists accounts
    (``WHERE ... AND NOT (<it>)``): the count and the limit of that query then
    leave such accounts out."""
    return (
        "NOT EXISTS (SELECT 1 FROM account_setting s_l"
        f" WHERE s_l.account_id = {account_id_column}"
        " AND s_l.name = 'listed' AND s_l.value = 'yes')"
    )


def _choices() -> str:
    return "; ".join(f"{s.name}: {' or '.join(s.values)}" for s in SETTINGS)


def _parse(text: str) -> list[tuple[Setting, str]]:
    changes: list[tuple[Setting, str]] = []
    for line in text.replace(";", "\n").splitlines():
        if not line.strip():
            continue
        name, colon, wanted = line.partition(":")
        setting = _BY_NAME.get(" ".join(name.lower().split()))
        wanted = wanted.strip().lower()
        if not colon or setting is None or wanted not in setting.values:
            raise ReticulaError(
                BAD_INPUT,
                f"cannot read {line.strip()[:60]!r} as a setting; the settings are"
                f" {_choices()}",
                hint='edit(ref="settings", text="word handles: on")',
            )
        changes.append((setting, wanted))
    if not changes:
        raise ReticulaError(
            BAD_INPUT,
            f"no setting given; the settings are {_choices()}",
            hint='edit(ref="settings", text="word handles: on")',
        )
    return changes


def change(conn: Conn, viewer: Account, text: str | None, **others: object) -> str:
    """``edit(ref="settings", text=...)``: one or more lines ``name: value``."""
    if any(v is not None for v in others.values()):
        raise ReticulaError(BAD_INPUT, 'settings take text only: "name: value" lines')
    changes = _parse(text or "")
    key = connection.current()
    if key is None and any(s.scope == "connection" for s, _ in changes):
        raise ReticulaError(
            BAD_INPUT,
            "word handles are set per connection, and this call has none",
        )
    for setting, wanted in changes:
        if setting.scope == "account":
            conn.execute(
                "INSERT INTO account_setting (account_id, name, value)"
                " VALUES (%s, %s, %s) ON CONFLICT (account_id, name)"
                " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (viewer.id, setting.key, wanted),
            )
        else:
            conn.execute(
                "INSERT INTO connection_setting (connection, account_id, name, value)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (connection, name)"
                " DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, viewer.id, setting.key, wanted),
            )
    return "\n".join(["Settings changed.", *lines(conn, viewer)])


def lines(conn: Conn, viewer: Account) -> list[str]:
    """The settings as profile shows them: value, scope, what it does."""
    out = []
    for s in SETTINGS:
        where = "this connection" if s.scope == "connection" else "this account"
        now = value(conn, viewer, s.name)
        out.append(f"- {s.name}: {now} ({where}): {s.says[now]}")
    first = SETTINGS[0]
    now = value(conn, viewer, first.name)
    flip = next(v for v in first.values if v != now)
    out.append(f'change one with edit(ref="settings", text="{first.name}: {flip}")')
    return out
