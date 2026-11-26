"""From 0.5.0 a new account is not listed in the directory (settings.py, the
default of "listed" is "no"). An account that existed before and never set it
was listed, and 0358 keeps it so. Checked by upgrading a database that has the
migrations of 0.4.2 only, as the live one has."""

from __future__ import annotations

import shutil
from pathlib import Path

from reticula_mcp import accounts, settings
from reticula_mcp.db import connect
from reticula_mcp.db.migrate import discover, migrate
from tests.conftest import PASSWORD

#: the last migration 0.4.2 shipped; 0304 and the files after 0355 are new
LAST_OF_042 = 355
NEW_SINCE_042 = {304, 305}


def test_an_upgrade_keeps_older_accounts_listed_and_new_ones_are_not(
    blank_db_url, tmp_path: Path
) -> None:
    old = tmp_path / "migrations-0.4.2"
    old.mkdir()
    for m in discover():
        if m.version <= LAST_OF_042 and m.version not in NEW_SINCE_042:
            shutil.copy(m.path, old / m.path.name)
    with connect(blank_db_url) as c:
        migrate(c, old)
        never = accounts.create_account(c, "never@example.com", PASSWORD, "never-set")
        said_no = accounts.create_account(c, "no@example.com", PASSWORD, "said-no")
        said_yes = accounts.create_account(c, "yes@example.com", PASSWORD, "said-yes")
        c.execute(
            "INSERT INTO account_setting (account_id, name, value)"
            " VALUES (%s, 'listed', 'no'), (%s, 'listed', 'yes')",
            (said_no.id, said_yes.id),
        )
        c.commit()

        ran = {m.version for m in migrate(c)}
        assert {304, 358} <= ran
        assert settings.listed(c, never) is True  # was listed, stays listed
        assert settings.listed(c, said_no) is False
        assert settings.listed(c, said_yes) is True

        new = accounts.create_account(c, "new@example.com", PASSWORD, "brand-new")
        assert settings.listed(c, new) is False
        listed = {
            r["handle"]
            for r in c.execute(
                "SELECT a.handle FROM account a"
                f" WHERE NOT ({settings.unlisted_sql('a.id')})"
            )
        }
        assert listed == {"never-set", "said-yes"}
        assert migrate(c) == []  # running it again changes nothing
