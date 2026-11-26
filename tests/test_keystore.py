"""Private keys encrypted at rest (keystore.py, migration 0355)."""

from __future__ import annotations

import ast
import base64
import os
import pathlib
import secrets
import stat
from collections.abc import Iterator
from typing import Any

import pytest

from reticula_mcp import accounts, cli, keystore
from reticula_mcp.db import connect
from reticula_mcp.errors import ReticulaError
from tests.conftest import PASSWORD, TEST_KEY_FILE

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "reticula_mcp"


@pytest.fixture(autouse=True)
def back_to_the_suite_secret() -> Iterator[None]:
    yield
    keystore.configure(TEST_KEY_FILE)


def _stored(conn: Any, account_id: int) -> dict[str, bytes]:
    return {
        r["key_id"]: bytes(r["private_key"])
        for r in conn.execute(
            "SELECT key_id, private_key FROM account_key WHERE account_id = %s",
            (account_id,),
        ).fetchall()
    }


def _key_file(tmp_path: pathlib.Path, name: str = "k", mode: int = 0o600) -> str:
    p = tmp_path / name
    p.write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
    p.chmod(mode)
    return str(p)


def test_new_keys_are_stored_encrypted_and_bound_to_their_row(conn: Any) -> None:
    acct = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
    stored = _stored(conn, acct.id)
    assert set(stored) == {"root", "k1", "e1"}
    for key_id, value in stored.items():
        assert len(value) == keystore.SEALED_LENGTH and value[0] == keystore.VERSION
        assert len(keystore.decrypt(acct.id, key_id, value)) == 32
    # moved to another slot or another account, it does not decrypt
    with pytest.raises(ReticulaError):
        keystore.decrypt(acct.id, "root", stored["k1"])
    with pytest.raises(ReticulaError):
        keystore.decrypt(acct.id + 1, "k1", stored["k1"])
    # tampered
    bad = stored["k1"][:-1] + bytes([stored["k1"][-1] ^ 1])
    with pytest.raises(ReticulaError):
        keystore.decrypt(acct.id, "k1", bad)


def test_the_column_takes_only_the_two_forms(conn: Any) -> None:
    acct = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
    with pytest.raises(Exception):  # noqa: B017  (CheckViolation)
        conn.execute(
            "UPDATE account_key SET private_key = %s WHERE account_id = %s",
            (b"\x02" * 65, acct.id),
        )


def test_signing_works_with_the_encrypted_key(conn: Any) -> None:
    from reticula_mcp.graph import handlers as graph

    acct = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
    assert "saved note" in graph.put(conn, acct, text="signed with an encrypted key")


def test_no_secret_refuses_and_none_is_an_explicit_opt_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(ReticulaError) as err:
        keystore.configure(None)
    assert "RETICULA_KEY_FILE is not set" in err.value.message
    keystore.configure("none")
    assert not keystore.encrypting()
    assert "unencrypted" in caplog.text


def test_the_key_file_must_be_the_owners_alone(tmp_path: pathlib.Path) -> None:
    for mode in (0o640, 0o604, 0o660, 0o644):
        path = _key_file(tmp_path, f"k{mode:o}", mode)
        with pytest.raises(ReticulaError) as err:
            keystore.read_secret(path)
        assert f"has mode {mode:04o}" in err.value.message
        assert "chmod 600" in (err.value.hint or "")
    assert len(keystore.read_secret(_key_file(tmp_path, "ok", 0o600))) == 32
    assert len(keystore.read_secret(_key_file(tmp_path, "ro", 0o400))) == 32
    with pytest.raises(ReticulaError) as err:
        keystore.read_secret(tmp_path / "missing")
    assert "does not exist" in err.value.message


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any file")
def test_an_unreadable_key_file_says_so(tmp_path: pathlib.Path) -> None:
    path = _key_file(tmp_path, "locked", 0o000)
    with pytest.raises(ReticulaError) as err:
        keystore.read_secret(path)
    assert err.value.message.startswith(f"cannot read '{path}' as uid {os.getuid()}")


def test_openssl_style_base64_with_whitespace_is_accepted(
    tmp_path: pathlib.Path,
) -> None:
    p = tmp_path / "openssl"
    raw = secrets.token_bytes(32)
    p.write_text("  " + base64.b64encode(raw).decode() + "\n\n")
    p.chmod(0o600)
    assert keystore.read_secret(p) == raw


def test_init_writes_a_private_file_and_never_overwrites(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "new.key"
    keystore.init_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(keystore.read_secret(path)) == 32
    with pytest.raises(FileExistsError):
        keystore.init_file(path)


def test_a_wrong_secret_refuses_to_start(
    fresh_db_url: str, tmp_path: pathlib.Path
) -> None:
    with connect(fresh_db_url) as conn:
        accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
        keystore.configure(_key_file(tmp_path, "other"))
        with pytest.raises(ReticulaError) as err:
            keystore.check_database(conn)
    assert "another secret" in err.value.message


def test_encrypt_converts_plaintext_keys_and_is_safe_to_run_again(
    fresh_db_url: str, tmp_path: pathlib.Path
) -> None:
    """An install from before 0.4.2: plaintext keys, then the upgrade step."""
    keystore.configure("none")
    with connect(fresh_db_url) as conn:
        ids = [
            accounts.create_account(conn, f"p{i}@example.com", PASSWORD).id
            for i in range(4)
        ]
        conn.commit()
        assert all(len(v) == 32 for i in ids for v in _stored(conn, i).values())
        keystore.configure(TEST_KEY_FILE)
        with pytest.raises(ReticulaError) as err:
            keystore.check_database(conn)
        assert "not encrypted" in err.value.message

        stop_after = ids[1]

        def interrupt(account_id: int) -> None:
            if account_id == stop_after:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            keystore.encrypt_all(conn, after_account=interrupt)
        conn.rollback()
        done = [i for i in ids if all(len(v) == 65 for v in _stored(conn, i).values())]
        assert done == ids[:2]  # each account all or nothing
        again = keystore.encrypt_all(conn)
        assert again.encrypted == 2 * 3 and again.already == 2 * 3
        third = keystore.encrypt_all(conn)
        assert third.encrypted == 0
        keystore.check_database(conn)
        for i in ids:
            for key_id, value in _stored(conn, i).items():
                assert len(keystore.decrypt(i, key_id, value)) == 32


def test_rotate_re_encrypts_with_the_new_secret(
    fresh_db_url: str, tmp_path: pathlib.Path
) -> None:
    with connect(fresh_db_url) as conn:
        acct = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
        conn.commit()
        before = {
            k: keystore.decrypt(acct.id, k, v)
            for k, v in _stored(conn, acct.id).items()
        }
        new = _key_file(tmp_path, "new")
        counts = keystore.rotate_all(conn, keystore.read_secret(new))
        assert counts.encrypted >= 3
        assert keystore.rotate_all(conn, keystore.read_secret(new)).encrypted == 0
        keystore.configure(new)
        keystore.check_database(conn)
        after = {
            k: keystore.decrypt(acct.id, k, v)
            for k, v in _stored(conn, acct.id).items()
        }
    assert before == after


def test_the_commands(
    fresh_db_url: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("RETICULA_DATABASE_URL", fresh_db_url)
    path = tmp_path / "cli.key"
    assert cli.main(["key", "init", "--file", str(path)]) == 0
    assert str(path) in capsys.readouterr().out
    assert cli.main(["key", "init", "--file", str(path)]) == 1  # never overwritten
    assert cli.main(["key", "encrypt"]) == 0
    assert "encrypted 0 keys" in capsys.readouterr().out
    monkeypatch.delenv("RETICULA_KEY_FILE")
    assert cli.main(["key", "encrypt"]) == 1
    assert "RETICULA_KEY_FILE is not set" in capsys.readouterr().err


def test_only_the_signing_path_account_creation_and_the_cli_import_keystore() -> None:
    allowed = {"accounts.py", "records.py", "cli.py", "server.py", "keystore.py"}
    users = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [
                    f"{node.module}.{a.name}" for a in node.names
                ]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.endswith("keystore") for n in names):
                users.add(str(path.relative_to(SRC)))
    assert users <= allowed, users - allowed
    # server.py only configures it at start; it never encrypts or decrypts
    server = (SRC / "server.py").read_text()
    assert "keystore.encrypt" not in server and "keystore.decrypt" not in server


def test_no_error_text_carries_key_bytes(conn: Any) -> None:
    acct = accounts.create_account(conn, "ana@example.com", PASSWORD, "ana")
    stored = _stored(conn, acct.id)["k1"]
    raw = keystore.decrypt(acct.id, "k1", stored)
    for bad in (stored[:-1] + b"\x00", b"\x01" + stored[1:5] + b"\x00" * 60):
        with pytest.raises(ReticulaError) as err:
            keystore.decrypt(acct.id, "k1", bad)
        text = err.value.render()
        assert raw.hex() not in text and stored.hex() not in text
