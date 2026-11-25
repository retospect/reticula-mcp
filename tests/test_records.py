from __future__ import annotations

import dataclasses

import psycopg
import pytest

from reticula_mcp import format as fmt
from reticula_mcp import records
from reticula_mcp.errors import ReticulaError


def atom(conn, acct, text="hello", **kw):
    addr = kw.pop("address", None) or fmt.new_address(acct.person_id)
    return records.sign_and_append(
        conn,
        acct,
        type="atom",
        address=addr,
        payload={"kind": "note", "text": text},
        **kw,
    )


def pub(conn, acct, key="k1"):
    return bytes(
        conn.execute(
            "SELECT public_key FROM account_key WHERE account_id=%s AND key_id=%s",
            (acct.id, key),
        ).fetchone()["public_key"]
    )


def test_sign_append_verify_and_export(conn, make_account):
    a = make_account()
    r = atom(conn, a, "Größe ≈ 3 µm")
    assert r.type == "atom" and r.key_id == "k1" and r.author == a.person_id
    assert r.parents == () and r.hub_id is None and r.audience == "hub"
    assert r.version_hash == fmt.version_hash(r.envelope)
    assert records.verify_record(conn, r)
    assert records.get_record(conn, r.version_hash) == r
    assert records.get_record(conn, r.id) == r
    assert fmt.verify_signed(r.text_form, pub(conn, a))
    assert r.payload == {"kind": "note", "text": "Größe ≈ 3 µm"}
    assert fmt.check_envelope(r.fields) == []


def test_versions_head_feed_and_paging(conn, make_account):
    a, b = make_account(), make_account()
    v1 = atom(conn, a, "one")
    v2 = atom(conn, a, "two", address=v1.address, parents=[v1.version_hash])
    other = atom(conn, b, "not mine")
    assert records.head(conn, v1.address) == v2
    assert records.versions(conn, v1.address) == [v1, v2]
    ids = [r.id for r in records.feed(conn, a.person_id)]
    assert ids == sorted(ids, reverse=True)
    assert other.id not in ids and {v1.id, v2.id} <= set(ids)
    assert [r.id for r in records.feed(conn, a.person_id, limit=1)] == [ids[0]]
    assert [
        r.id for r in records.feed(conn, a.person_id, after_id=ids[0], limit=1)
    ] == [ids[1]]
    assert [r.id for r in records.feed(conn, a.person_id, since_id=v1.id)] == [v2.id]
    assert records.head(conn, fmt.new_address(a.person_id)) is None


def test_local_write_rules(conn, make_account):
    a, b = make_account(), make_account()
    v1 = atom(conn, a)
    with pytest.raises(ReticulaError) as e:  # same address twice without a parent
        atom(conn, a, address=v1.address)
    assert e.value.code == "Conflict"
    with pytest.raises(ReticulaError) as e:  # parent that does not exist
        atom(conn, a, address=v1.address, parents=["sha256:" + "0" * 64])
    assert e.value.code == "Conflict"
    with pytest.raises(ReticulaError) as e:  # someone else's address
        atom(conn, b, address=v1.address, parents=[v1.version_hash])
    assert e.value.code == "Forbidden"
    with pytest.raises(ReticulaError) as e:  # malformed address
        atom(conn, a, address="nonsense")
    assert e.value.code == "BadInput"
    tomb = records.sign_and_append(
        conn, a, type="tombstone", address=v1.address, payload={"derived": False},
        parents=[v1.version_hash],
    )  # fmt: skip
    with pytest.raises(ReticulaError) as e:  # terminal
        atom(conn, a, address=v1.address, parents=[tomb.version_hash])
    assert e.value.code == "Conflict"


def test_key_roles(conn, make_account):
    a = make_account()
    addr = fmt.new_address(a.person_id)
    with pytest.raises(ReticulaError) as e:  # atoms are signed by the delegate
        records.sign_and_append(
            conn, a, type="atom", address=addr, payload={"text": "x"}, key="root"
        )
    assert e.value.code == "BadInput"
    with pytest.raises(ReticulaError) as e:  # identity needs the root key
        records.sign_and_append(
            conn, a, type="identity", address=addr, payload={}, key="k1"
        )
    assert e.value.code == "BadInput"
    with pytest.raises(ReticulaError) as e:  # encryption slot cannot sign
        records.sign_and_append(
            conn, a, type="atom", address=addr, payload={"text": "x"}, key="e1"
        )
    assert e.value.code in ("NotFound", "BadInput")
    conn.execute(
        "UPDATE account_key SET revoked_at = now() WHERE account_id = %s AND key_id = 'k1'",
        (a.id,),
    )
    with pytest.raises(ReticulaError) as e:
        atom(conn, a)
    assert e.value.code == "Forbidden"


def test_unknown_type_and_fields_round_trip_byte_identical(conn, make_account):
    a = make_account()
    priv = bytes(
        conn.execute(
            "SELECT private_key FROM account_key WHERE account_id=%s AND key_id='k1'",
            (a.id,),
        ).fetchone()["private_key"]
    )
    cases = [
        dict(type="x-future", extra={"zeta": [1, {"b": None, "a": "β"}], "alpha": "ünknown"},
             payload={"whatever": ["future", {"shape": True}]}, audience="x-circle"),
        dict(type="atom", extra={"sidecar": {"n": 1}},
             payload={"kind": "flux-capacitor", "text": "t", "novel": [1, 2]}, audience="hub"),
    ]  # fmt: skip
    for case in cases:
        env = fmt.build_envelope(
            address=fmt.new_address(a.person_id),
            author=a.person_id,
            key="k1",
            created=fmt.format_created(),
            **case,
        )
        sig = fmt.sign(priv, env)
        stored = records.append_signed(conn, env, sig)
        fetched = records.get_record(conn, stored.version_hash)
        assert fetched.envelope == env and fetched.signature == sig  # same bytes
        raw = conn.execute(
            "SELECT envelope FROM record WHERE id=%s", (stored.id,)
        ).fetchone()
        assert bytes(raw["envelope"]) == env
        assert records.verify_record(conn, fetched)
        assert fmt.verify_signed(fetched.text_form, pub(conn, a))
        assert fmt.decode_signed(fetched.text_form) == (env, sig)
        assert fetched.type == case["type"] and fetched.audience == case["audience"]
        assert fetched.fields["alpha" if "alpha" in case["extra"] else "sidecar"]


def test_non_canonical_but_valid_envelope_is_stored_as_received(conn, make_account):
    """We never re-serialise: bytes in = bytes out, even with odd whitespace/order."""
    import json

    a = make_account()
    priv = bytes(
        conn.execute(
            "SELECT private_key FROM account_key WHERE account_id=%s AND key_id='k1'",
            (a.id,),
        ).fetchone()["private_key"]
    )
    canonical = fmt.build_envelope(
        type="atom", address=fmt.new_address(a.person_id), author=a.person_id, key="k1",
        created=fmt.format_created(), payload={"kind": "note", "text": "x"},
    )  # fmt: skip
    odd = json.dumps(json.loads(canonical), indent=2, ensure_ascii=True).encode()
    assert odd != canonical
    stored = records.append_signed(conn, odd, fmt.sign(priv, odd))
    assert records.get_record(conn, stored.id).envelope == odd
    assert records.verify_record(conn, stored)


def test_append_signed_rejects_bad_input(conn, make_account):
    a, b = make_account(), make_account()
    env = fmt.build_envelope(
        type="atom", address=fmt.new_address(a.person_id), author=a.person_id, key="k1",
        created=fmt.format_created(), payload={"text": "x"},
    )  # fmt: skip
    bpriv = bytes(
        conn.execute(
            "SELECT private_key FROM account_key WHERE account_id=%s AND key_id='k1'",
            (b.id,),
        ).fetchone()["private_key"]
    )
    for bad_env, bad_sig in [
        (env, fmt.sign(bpriv, env)),  # signed by the wrong person
        (env, b"\x00" * 64),
        (env + b" ", fmt.sign(bpriv, env)),
        (b"not json", b"\x00" * 64),
        (b"[1]", b"\x00" * 64),
        (env.replace(b'"v":0', b'"v":1'), b"\x00" * 64),
    ]:
        with pytest.raises(ReticulaError) as e:
            records.append_signed(conn, bad_env, bad_sig)
        assert e.value.code == "BadInput"
    # duplicates are a Conflict and do not poison the transaction
    r = atom(conn, a)
    with pytest.raises(ReticulaError) as e:
        records.append_signed(conn, r.envelope, r.signature)
    assert e.value.code == "Conflict"
    assert records.get_record(conn, r.id) == r


def test_check_record_detects_tampering(conn, make_account):
    a = make_account()
    r = atom(conn, a, "payload")
    assert records.check_record(conn, r) == []
    flipped = bytearray(r.envelope)
    flipped[-5] ^= 1
    tampered = dataclasses.replace(r, envelope=bytes(flipped))
    problems = records.check_record(conn, tampered)
    assert any(
        "version_hash" in p or "envelope" in p or "signature" in p for p in problems
    )
    assert not records.verify_record(conn, tampered)
    bad_sig = dataclasses.replace(r, signature=bytes(64))
    assert "signature does not verify" in records.check_record(conn, bad_sig)
    wrong_column = dataclasses.replace(r, audience="private")
    assert any("audience" in p for p in records.check_record(conn, wrong_column))


def test_blank_after_tombstone_is_the_only_erasure(conn, make_account):
    def blank(rec_id):
        return conn.execute(
            "SELECT blank_record_content(%s) AS ok", (rec_id,)
        ).fetchone()["ok"]

    a = make_account()
    v1 = atom(conn, a, "secret")
    with pytest.raises(psycopg.errors.RaiseException, match="no tombstone"):
        with conn.transaction():
            blank(v1.id)
    tomb = records.sign_and_append(
        conn, a, type="tombstone", address=v1.address, payload={"derived": False},
        parents=[v1.version_hash],
    )  # fmt: skip
    assert blank(v1.id) is True
    erased = records.get_record(conn, v1.id)
    assert erased.blanked and erased.envelope == b"" and erased.signature == b""
    assert erased.version_hash == v1.version_hash and erased.address == v1.address
    assert records.check_record(conn, erased) == ["content was erased (tombstoned)"]
    assert blank(v1.id) is False  # already blank
    assert blank(tomb.id) is False  # the tombstone itself is never erased
    assert records.verify_record(conn, tomb)
    with pytest.raises(
        psycopg.errors.RaiseException, match="append-only"
    ):  # flag did not leak
        with conn.transaction():
            conn.execute("UPDATE record SET envelope = '\\x' WHERE id = %s", (tomb.id,))
