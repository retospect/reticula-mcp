#!/usr/bin/env python3
"""Regenerate tests/vectors/v0.json from the format library.

Run only when format v0 is deliberately changed (it is frozen: that means
never, in practice). The conformance test recomputes every value here with
the library AND with the independent implementation in tests/reference.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from reticula_mcp import format as fmt

OUT = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "v0.json"

SEEDS = {
    "alice.root": bytes([1]) * 32,
    "alice.k1": bytes([2]) * 32,
    "bob.root": bytes([3]) * 32,
    "bob.k1": bytes([4]) * 32,
}
T0 = 1796000000000  # 2026-11-30T00:53:20.000Z


def main() -> None:
    keys: dict[str, dict[str, str]] = {}
    pubs: dict[str, bytes] = {}
    for name, seed in SEEDS.items():
        _, pub = fmt.generate_signing_key(seed)
        pubs[name] = pub
        keys[name] = {
            "seed_hex": seed.hex(),
            "public_hex": pub.hex(),
            "public_b64u": fmt.b64u(pub),
        }
    alice = fmt.person_id_from_root_key(pubs["alice.root"])
    bob = fmt.person_id_from_root_key(pubs["bob.root"])
    persons = [
        {"name": "alice", "root_key": "alice.root", "expected_person_id": alice},
        {"name": "bob", "root_key": "bob.root", "expected_person_id": bob},
    ]

    uuid_cases = []
    addrs: dict[str, str] = {}
    for i, label in enumerate(["identity", "note", "hub", "member", "other", "paper"]):
        ts = T0 + i * 1000
        rnd = bytes((i * 17 + j * 5 + 3) % 256 for j in range(10))
        u = fmt.uuid7(ts, rnd)
        uuid_cases.append(
            {"label": label, "timestamp_ms": ts, "random_hex": rnd.hex(), "expected": u}
        )
        addrs[label] = fmt.format_address(alice, u)
    # extreme cases: all-zero and all-one random bytes
    for label, ts, rnd in [
        ("zeros", 0, bytes(10)),
        ("ones", (1 << 48) - 1, bytes([255]) * 10),
    ]:
        uuid_cases.append(
            {
                "label": label,
                "timestamp_ms": ts,
                "random_hex": rnd.hex(),
                "expected": fmt.uuid7(ts, rnd),
            }
        )
    hub_cases = [
        {
            "hub_address": addrs["hub"],
            "expected": fmt.hub_id_from_address(addrs["hub"]),
        },
        {"hub_address": "ret:x/y", "expected": fmt.hub_id_from_address("ret:x/y")},
    ]
    hub_id = hub_cases[0]["expected"]

    canon_cases = [
        {
            "input": {"b": 1, "a": [True, None, "x"]},
            "expected": '{"a":[true,null,"x"],"b":1}',
        },
        {
            "input": {"t": "Größe ≈ 3 µm 日本語 🙂"},
            "expected": '{"t":"Größe ≈ 3 µm 日本語 🙂"}',
        },
        {
            "input": {"q": 'a"b\\c\n\t\u0001'},
            "expected": '{"q":"a\\"b\\\\c\\n\\t\\u0001"}',
        },
        {
            "input": {"é": 1, "e": 2, "z": {"y": 0, "x": -5}},
            "expected": '{"e":2,"z":{"x":-5,"y":0},"é":1}',
        },
    ]
    for c in canon_cases:
        assert fmt.canonical_text(c["input"]) == c["expected"], c

    records: list[dict] = []
    heads: dict[str, str] = {}

    def add(name: str, signer: str, **inp) -> str:
        parents = [heads[p] for p in inp.pop("parent_names", [])]
        inputs = {
            "type": inp["type"],
            "address": inp["address"],
            "author": alice,
            "key": signer.split(".")[1],
            "created": inp["created"],
            "parents": parents,
            "hub": inp.get("hub"),
            "audience": inp.get("audience", "hub"),
            "payload": inp["payload"],
        }
        if "extra" in inp:
            inputs["extra"] = inp["extra"]
        env = fmt.build_envelope(**inputs)
        sig = fmt.sign(SEEDS[signer], env)
        payload_text = fmt.parse_envelope(env)["payload"]
        vh = fmt.version_hash(env)
        records.append(
            {
                "name": name,
                "signer": signer,
                "inputs": inputs,
                "expected": {
                    "payload_text": payload_text,
                    "content_hash": fmt.content_hash(payload_text.encode("utf-8")),
                    "envelope_text": env.decode("utf-8"),
                    "envelope_b64u": fmt.b64u(env),
                    "version_hash": vh,
                    "signature_b64u": fmt.b64u(sig),
                    "text_form": fmt.encode_signed(env, sig),
                },
            }
        )
        heads[name] = vh
        return vh

    key_entries = [
        {
            "id": "root",
            "use": "sign",
            "role": "root",
            "alg": "ed25519",
            "pub": keys["alice.root"]["public_b64u"],
        },
        {
            "id": "k1",
            "use": "sign",
            "role": "delegate",
            "alg": "ed25519",
            "pub": keys["alice.k1"]["public_b64u"],
        },
        {
            "id": "e1",
            "use": "encrypt",
            "alg": "x25519",
            "pub": fmt.b64u(bytes(range(32))),
        },
    ]
    add(
        "identity-v1",
        "alice.root",
        type="identity",
        address=addrs["identity"],
        created=fmt.format_created(T0),
        audience="public",
        payload={
            "handle": "alice",
            "keys": key_entries,
            "verified": {"value": False, "at": None},
        },
    )
    add(
        "identity-v2-verified",
        "alice.root",
        type="identity",
        address=addrs["identity"],
        created=fmt.format_created(T0 + 500),
        audience="public",
        parent_names=["identity-v1"],
        payload={
            "handle": "alice",
            "keys": key_entries,
            "verified": {"value": True, "at": fmt.format_created(T0 + 400)},
        },
    )
    add(
        "hub-atom",
        "alice.k1",
        type="atom",
        address=addrs["hub"],
        created=fmt.format_created(T0 + 1000),
        payload={
            "kind": "hub",
            "title": "Lab notes",
            "text": "Shared notes for the lab.",
            "preset": "group",
            "invite": "members",
            "accept": "members",
        },
    )
    add(
        "note-v1",
        "alice.k1",
        type="atom",
        address=addrs["note"],
        created=fmt.format_created(T0 + 2000),
        hub=hub_id,
        payload={
            "kind": "note",
            "title": "Größe",
            "text": "Pore Größe ≈ 3 µm — 日本語 🙂",
            "tags": ["materials", "porosity"],
            "origin": {"by": "agent", "model": "test-model"},
        },
    )
    add(
        "note-v2",
        "alice.k1",
        type="atom",
        address=addrs["note"],
        created=fmt.format_created(T0 + 3000),
        hub=hub_id,
        parent_names=["note-v1"],
        payload={
            "kind": "note",
            "title": "Größe",
            "text": "Pore size ≈ 3.2 µm (corrected)",
            "links": [{"rel": "part-of", "to": addrs["hub"]}],
        },
    )
    add(
        "note-private",
        "alice.k1",
        type="atom",
        address=addrs["other"],
        created=fmt.format_created(T0 + 3500),
        hub=hub_id,
        audience="private",
        payload={"kind": "debug", "text": "only me"},
    )
    add(
        "member-v1",
        "alice.k1",
        type="member",
        address=addrs["member"],
        created=fmt.format_created(T0 + 4000),
        hub=hub_id,
        payload={"atom": addrs["note"], "hub": addrs["hub"], "withdrawn": False},
    )
    add(
        "member-v2-withdrawn",
        "alice.k1",
        type="member",
        address=addrs["member"],
        created=fmt.format_created(T0 + 5000),
        hub=hub_id,
        parent_names=["member-v1"],
        payload={"atom": addrs["note"], "hub": addrs["hub"], "withdrawn": True},
    )
    add(
        "grant-bob",
        "alice.k1",
        type="grant",
        address=fmt.format_address(alice, fmt.uuid7(T0 + 6000, bytes(range(10)))),
        created=fmt.format_created(T0 + 6000),
        hub=hub_id,
        payload={"hub": addrs["hub"], "to": bob, "mode": "add", "wrapped_key": None},
    )
    add(
        "tombstone",
        "alice.k1",
        type="tombstone",
        address=addrs["note"],
        created=fmt.format_created(T0 + 7000),
        hub=hub_id,
        parent_names=["note-v2"],
        payload={"derived": False},
    )
    add(
        "unknown-type-with-extras",
        "alice.k1",
        type="x-future",
        address=fmt.format_address(alice, fmt.uuid7(T0 + 8000, bytes(range(10, 20)))),
        created=fmt.format_created(T0 + 8000),
        audience="x-circle",
        extra={"zeta": [1, {"b": None, "a": "β"}], "alpha": "ünknown"},
        payload={"whatever": ["future", {"shape": True}]},
    )
    add(
        "atom-unknown-kind-and-fields",
        "alice.k1",
        type="atom",
        address=addrs["paper"],
        created=fmt.format_created(T0 + 9000),
        hub=hub_id,
        extra={"sidecar": {"n": 1}},
        payload={
            "kind": "flux-capacitor",
            "text": "t",
            "ext": {"doi": "10.1000/x"},
            "novel": [1, 2, 3],
        },
    )

    by_name = {r["name"]: r for r in records}
    good = by_name["note-v1"]["expected"]["text_form"]
    env_b, sig_b = fmt.decode_signed(good)
    flipped_env = bytearray(env_b)
    flipped_env[len(flipped_env) // 2] ^= 0x01
    flipped_sig = bytearray(sig_b)
    flipped_sig[0] ^= 0x80
    failures = [
        {
            "name": "control-valid",
            "signer_key": "alice.k1",
            "text_form": good,
            "expect_valid": True,
        },
        {
            "name": "flipped-envelope-byte",
            "signer_key": "alice.k1",
            "text_form": fmt.encode_signed(bytes(flipped_env), sig_b),
            "expect_valid": False,
        },
        {
            "name": "flipped-signature-byte",
            "signer_key": "alice.k1",
            "text_form": fmt.encode_signed(env_b, bytes(flipped_sig)),
            "expect_valid": False,
        },
        {
            "name": "wrong-key",
            "signer_key": "bob.k1",
            "text_form": good,
            "expect_valid": False,
        },
        {
            "name": "malformed-no-dot",
            "signer_key": "alice.k1",
            "text_form": good.replace(".", ""),
            "expect_valid": False,
        },
    ]
    for f in failures:
        assert (
            fmt.verify_signed(f["text_form"], pubs[f["signer_key"]])
            == f["expect_valid"]
        ), f

    doc = {
        "name": "reticula-format-vectors",
        "format_version": 0,
        "description": (
            "Golden vectors for Reticula note format v0 (docs/format.md). Every 'expected' value must be "
            "reproduced bit for bit by an implementation. Seeds are PUBLIC TEST VALUES, never use them for real keys."
        ),
        "conventions": {
            "b64u": "base64url (RFC 4648 s5), no padding",
            "b32": "RFC 4648 base32, lowercase, no padding",
            "ed25519": "RFC 8032 pure Ed25519; 'seed_hex' is the 32-byte private seed; signatures are 64 raw bytes",
            "canonical_json": "UTF-8, keys sorted by code point, separators ',' ':' , no whitespace, non-ASCII emitted raw, no floats/NaN",
            "person_id": "'p' + b32(sha256(b'reticula/person/v0\\x00' + root_pub32))[:26]",
            "hub_id": "'h' + b32(sha256(b'reticula/hub/v0\\x00' + utf8(hub_address)))[:26]",
            "uuid7": "48-bit big-endian ms timestamp | 0x7 | rand_a(12) | 0b10 | rand_b(62); random_hex is 10 bytes: rand_a = low nibble of byte0 + byte1, rand_b = low 6 bits of byte2 + bytes 3..9",
            "content_hash": "'sha256:' + hex(sha256(salt_bytes + utf8(payload string)))  (salt_bytes empty while salt is 'none')",
            "version_hash": "'sha256:' + hex(sha256(envelope_bytes))",
            "text_form": "b64u(envelope) + '.' + b64u(signature)",
            "record_inputs": "'inputs' are the arguments of build_envelope (plus 'extra' = unknown fields added to the envelope); 'payload' is an object serialised canonically into the envelope's 'payload' string",
        },
        "keys": keys,
        "persons": persons,
        "uuid7": uuid_cases,
        "hub_ids": hub_cases,
        "canonical_json": canon_cases,
        "records": records,
        "verification": failures,
    }
    OUT.write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT} ({len(records)} records)")


if __name__ == "__main__":
    main()
