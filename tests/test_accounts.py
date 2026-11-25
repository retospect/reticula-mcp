from __future__ import annotations

from datetime import timedelta

import pytest

from reticula_mcp import accounts, records
from reticula_mcp import format as fmt
from reticula_mcp.content import hooks
from reticula_mcp.errors import ReticulaError
from tests.conftest import PASSWORD


def keys(conn, account):
    rows = conn.execute(
        "SELECT * FROM account_key WHERE account_id = %s ORDER BY id", (account.id,)
    ).fetchall()
    return {r["key_id"]: r for r in rows}


def test_create_account_everything(conn):
    a = accounts.create_account(conn, "Ada.Lovelace+x@Example.com", PASSWORD)
    assert a.handle == "ada-lovelace"
    assert a.status == "active" and a.verified is False
    k = keys(conn, a)
    assert set(k) == {"root", "k1", "e1"}
    assert (k["root"]["alg"], k["root"]["use"], k["root"]["role"]) == (
        "ed25519",
        "sign",
        "root",
    )
    assert (k["k1"]["alg"], k["k1"]["role"]) == ("ed25519", "delegate")
    assert (k["e1"]["alg"], k["e1"]["use"], k["e1"]["role"]) == (
        "x25519",
        "encrypt",
        None,
    )
    assert a.person_id == fmt.person_id_from_root_key(bytes(k["root"]["public_key"]))
    # the signed identity record
    rec = records.head(conn, a.identity_address)
    assert rec is not None and rec.type == "identity" and rec.key_id == "root"
    assert rec.author == a.person_id and rec.parents == () and rec.audience == "public"
    assert records.verify_record(conn, rec)
    payload = rec.payload
    assert payload["handle"] == "ada-lovelace"
    assert payload["verified"] == {"value": False, "at": None}
    assert [(x["id"], x["use"], x.get("role")) for x in payload["keys"]] == [
        ("root", "sign", "root"), ("k1", "sign", "delegate"), ("e1", "encrypt", None)
    ]  # fmt: skip
    assert payload["keys"][0]["pub"] == fmt.b64u(bytes(k["root"]["public_key"]))
    assert (
        conn.execute("SELECT password_hash FROM account WHERE id=%s", (a.id,))
        .fetchone()["password_hash"]
        .startswith("$argon2id$")
    )


def test_handles_are_derived_unique_and_reserved_refused(conn, make_account):
    first = make_account("sam@one.example")
    second = make_account("sam@two.example")
    third = make_account("sam@three.example")
    assert (first.handle, second.handle, third.handle) == ("sam", "sam2", "sam3")
    assert make_account("al@x.example").handle == "al-user"
    assert make_account("admin@x.example").handle == "admin2"
    assert make_account("日本@x.example").handle == "user"
    assert make_account("n@x.example", handle="Custom_Name").handle == "custom_name"
    with pytest.raises(ReticulaError) as e:
        make_account("r@x.example", handle="reticula")
    assert e.value.code == "BadInput"
    for bad in ("ab", "has space", "x" * 33, "bad!"):
        with pytest.raises(ReticulaError) as e:
            make_account(f"{abs(hash(bad))}@x.example", handle=bad)
        assert e.value.code == "BadInput"
    with pytest.raises(ReticulaError) as e:
        make_account("t@x.example", handle="sam")
    assert e.value.code == "Conflict"


def test_input_validation_and_duplicate_email(conn, make_account):
    make_account("dup@example.com")
    with pytest.raises(ReticulaError) as e:
        accounts.create_account(conn, "DUP@example.com", PASSWORD)
    assert e.value.code == "Conflict"
    with pytest.raises(ReticulaError) as e:
        accounts.create_account(conn, "not-an-email", PASSWORD)
    assert e.value.code == "BadInput"
    with pytest.raises(ReticulaError) as e:
        accounts.create_account(conn, "short@example.com", "short")
    assert e.value.code == "BadInput"
    # the failed sign-ups left nothing behind and the connection still works
    assert accounts.get_account_by_email(conn, "short@example.com") is None


def test_on_account_created_hook_runs_in_the_transaction(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(
        hooks, "on_account_created", lambda c, acct: seen.append(acct.handle)
    )
    accounts.create_account(conn, "hooked@example.com", PASSWORD)
    assert seen == ["hooked"]


def test_authenticate(conn, make_account):
    a = make_account("login@example.com")
    assert accounts.authenticate(conn, "LOGIN@example.com", PASSWORD) == a
    assert accounts.authenticate(conn, "login@example.com", "wrong password!") is None
    assert accounts.authenticate(conn, "nobody@example.com", PASSWORD) is None
    conn.execute("UPDATE account SET status = 'disabled' WHERE id = %s", (a.id,))
    assert accounts.authenticate(conn, "login@example.com", PASSWORD) is None


def test_system_account(conn, make_account):
    s = accounts.ensure_system_account(conn)
    assert (s.handle, s.status, s.verified) == ("reticula", "system", True)
    assert accounts.ensure_system_account(conn) == s
    assert records.verify_record(conn, records.head(conn, s.identity_address))
    assert accounts.authenticate(conn, s.email, "") is None
    assert accounts.authenticate(conn, s.email, "!") is None
    with pytest.raises(ReticulaError):
        make_account("sys2@example.com", handle="reticula")
    assert make_account("reticula@example.com").handle == "reticula2"


def test_set_handle_appends_identity_version(conn, make_account):
    a = make_account("ren@example.com")
    other = make_account("taken@example.com")
    v1 = records.head(conn, a.identity_address)
    b = accounts.set_handle(conn, a, "@Renamed")
    assert b.handle == "renamed" and b.person_id == a.person_id
    v2 = records.head(conn, a.identity_address)
    assert v2.parents == (v1.version_hash,) and v2.payload["handle"] == "renamed"
    assert records.verify_record(conn, v2)
    assert accounts.get_account_by_handle(conn, "@renamed") == b
    assert accounts.set_handle(conn, b, "renamed") == b  # no-op, no new version
    assert len(records.versions(conn, a.identity_address)) == 2
    with pytest.raises(ReticulaError) as e:
        accounts.set_handle(conn, b, other.handle)
    assert e.value.code == "Conflict"
    with pytest.raises(ReticulaError) as e:
        accounts.set_handle(conn, b, "admin")
    assert e.value.code == "BadInput"


def test_set_verified_appends_identity_version(conn, make_account):
    a = make_account("ver@example.com")
    b = accounts.set_verified(conn, a)
    assert b.verified and b.verified_at is not None
    payload = records.head(conn, a.identity_address).payload
    assert payload["verified"]["value"] is True and payload["verified"]["at"]


def test_tokens(conn, make_account):
    a = make_account("tok@example.com")
    t = accounts.mint_token(conn, a, "laptop")
    assert t.startswith("ret_")
    stored = conn.execute(
        "SELECT * FROM oauth_token WHERE account_id = %s", (a.id,)
    ).fetchall()
    assert len(stored) == 1 and t not in str(stored)  # stored hashed
    assert stored[0]["token_hash"] == accounts.hash_token(t)
    assert stored[0]["label"] == "laptop" and stored[0]["last_used_at"] is None
    assert accounts.account_for_token(conn, t) == a
    assert conn.execute("SELECT last_used_at FROM oauth_token").fetchone()[
        "last_used_at"
    ]
    assert accounts.account_for_token(conn, t + "x") is None
    assert accounts.account_for_token(conn, "") is None
    # revoked, expired, refresh-kind, disabled account
    conn.execute("UPDATE oauth_token SET revoked_at = now()")
    assert accounts.account_for_token(conn, t) is None
    t2 = accounts.mint_token(conn, a, "short", ttl=timedelta(seconds=-5))
    assert accounts.account_for_token(conn, t2) is None
    t3 = accounts.mint_token(conn, a, "ok", ttl=timedelta(hours=1))
    assert accounts.account_for_token(conn, t3) == a
    conn.execute(
        "UPDATE oauth_token SET kind = 'refresh' WHERE token_hash = %s",
        (accounts.hash_token(t3),),
    )
    assert accounts.account_for_token(conn, t3) is None
    t4 = accounts.mint_token(conn, a, "again")
    conn.execute("UPDATE account SET status = 'disabled' WHERE id = %s", (a.id,))
    assert accounts.account_for_token(conn, t4) is None


def test_lookups(conn, make_account):
    a = make_account("look@example.com")
    assert accounts.get_account(conn, a.id) == a
    assert accounts.get_account_by_person_id(conn, a.person_id) == a
    assert accounts.get_account_by_handle(conn, "LOOK") == a
    assert accounts.get_account_by_email(conn, " look@example.com ") == a
    assert accounts.get_account(conn, -1) is None
