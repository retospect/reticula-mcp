"""Golden vectors: library, independent reference and file must agree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reticula_mcp import format as fmt
from tests import reference as ref

VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "v0.json").read_text(encoding="utf-8")
)
SEEDS = {n: bytes.fromhex(k["seed_hex"]) for n, k in VECTORS["keys"].items()}
PUBS = {n: bytes.fromhex(k["public_hex"]) for n, k in VECTORS["keys"].items()}


def test_vector_file_is_self_describing():
    assert VECTORS["format_version"] == 0
    for section in ("conventions", "keys", "persons", "uuid7", "hub_ids", "records", "verification"):
        assert VECTORS[section]
    types = {r["inputs"]["type"] for r in VECTORS["records"]}
    assert {"atom", "member", "grant", "tombstone", "identity"} <= types
    assert types - set(fmt.KNOWN_TYPES), "needs an unknown-type vector"
    assert any(r["inputs"]["parents"] for r in VECTORS["records"])
    assert any(not r["expected"]["envelope_text"].isascii() for r in VECTORS["records"])


@pytest.mark.parametrize("key", sorted(VECTORS["keys"]))
def test_public_keys(key):
    pub = fmt.generate_signing_key(SEEDS[key])[1]
    assert pub.hex() == VECTORS["keys"][key]["public_hex"]
    assert fmt.b64u(pub) == VECTORS["keys"][key]["public_b64u"] == ref.b64u(pub)


@pytest.mark.parametrize("p", VECTORS["persons"], ids=lambda p: p["name"])
def test_person_ids(p):
    pub = PUBS[p["root_key"]]
    assert fmt.person_id_from_root_key(pub) == p["expected_person_id"]
    assert ref.person_id(pub) == p["expected_person_id"]
    assert fmt.is_person_id(p["expected_person_id"])


@pytest.mark.parametrize("c", VECTORS["uuid7"], ids=lambda c: c["label"])
def test_uuid7(c):
    rnd = bytes.fromhex(c["random_hex"])
    assert fmt.uuid7(c["timestamp_ms"], rnd) == c["expected"]
    assert ref.uuid7(c["timestamp_ms"], rnd) == c["expected"]
    assert fmt.is_uuid7(c["expected"])


@pytest.mark.parametrize("c", VECTORS["hub_ids"], ids=lambda c: c["hub_address"][:12])
def test_hub_ids(c):
    assert fmt.hub_id_from_address(c["hub_address"]) == c["expected"]
    assert ref.hub_id(c["hub_address"]) == c["expected"]


@pytest.mark.parametrize("c", VECTORS["canonical_json"])
def test_canonical_json(c):
    assert fmt.canonical_text(c["input"]) == c["expected"]
    assert ref.canon(c["input"]).decode() == c["expected"]


@pytest.mark.parametrize("rec", VECTORS["records"], ids=lambda r: r["name"])
def test_records(rec):
    inputs, exp = rec["inputs"], rec["expected"]
    env = fmt.build_envelope(**inputs)
    assert env.decode("utf-8") == exp["envelope_text"]
    assert fmt.b64u(env) == exp["envelope_b64u"]
    assert env == ref.envelope(inputs)
    parsed = fmt.parse_envelope(env)
    assert parsed["payload"] == exp["payload_text"]
    assert parsed["content_hash"] == exp["content_hash"]
    assert fmt.content_hash(exp["payload_text"].encode()) == exp["content_hash"]
    assert fmt.version_hash(env) == exp["version_hash"]
    sig = fmt.sign(SEEDS[rec["signer"]], env)
    assert fmt.b64u(sig) == exp["signature_b64u"]
    assert sig == ref.signature(SEEDS[rec["signer"]], env)
    text = fmt.encode_signed(env, sig)
    assert text == exp["text_form"]
    assert fmt.decode_signed(text) == (env, sig)
    assert fmt.verify_signed(text, PUBS[rec["signer"]])
    if inputs["type"] in fmt.KNOWN_TYPES:
        assert fmt.check_envelope(parsed) == []
    assert fmt.is_valid_address(parsed["address"])


def test_parents_cover_version_hash():
    by_name = {r["name"]: r for r in VECTORS["records"]}
    v1, v2 = by_name["note-v1"], by_name["note-v2"]
    assert v2["inputs"]["parents"] == [v1["expected"]["version_hash"]]
    assert v1["inputs"]["address"] == v2["inputs"]["address"]
    assert v2["expected"]["version_hash"] != v1["expected"]["version_hash"]


def test_unknown_type_and_extras_are_tolerated():
    rec = next(r for r in VECTORS["records"] if r["name"] == "unknown-type-with-extras")
    parsed = fmt.parse_envelope(rec["expected"]["envelope_text"].encode())
    assert parsed["type"] == "x-future"
    assert parsed["alpha"] == "ünknown"
    assert fmt.check_envelope(parsed) == []
    assert fmt.verify_signed(rec["expected"]["text_form"], PUBS["alice.k1"])


@pytest.mark.parametrize("c", VECTORS["verification"], ids=lambda c: c["name"])
def test_verification(c):
    assert fmt.verify_signed(c["text_form"], PUBS[c["signer_key"]]) is c["expect_valid"]


def test_check_envelope_reports_real_problems():
    rec = next(r for r in VECTORS["records"] if r["name"] == "note-v1")
    good = fmt.parse_envelope(rec["expected"]["envelope_text"].encode())
    assert fmt.check_envelope(good) == []
    bad = dict(good, payload=good["payload"] + " ")
    assert any("content_hash" in p for p in fmt.check_envelope(bad))
    other = fmt.format_address(
        VECTORS["persons"][1]["expected_person_id"], good["address"].split("/")[1]
    )
    assert any("person part" in p for p in fmt.check_envelope(dict(good, address=other)))
    assert fmt.check_envelope({"v": 0})


def test_canonical_json_rejects_floats_and_nan():
    for bad in ({"a": 1.5}, [float("nan")], {"a": {"b": [0.0]}}):
        with pytest.raises(ValueError):
            fmt.canonical_json(bad)


def test_b64u_is_strict():
    assert fmt.b64u_decode(fmt.b64u(b"\xff\xfe")) == b"\xff\xfe"
    for bad in ("ab=", "a+b/", "abcde", " "):
        with pytest.raises(ValueError):
            fmt.b64u_decode(bad)


def test_address_parse_format():
    pid = VECTORS["persons"][0]["expected_person_id"]
    u = VECTORS["uuid7"][0]["expected"]
    addr = fmt.format_address(pid, u)
    assert addr == f"ret:{pid}/{u}"
    assert fmt.parse_address(addr) == fmt.Address(pid, u)
    for bad in ("", "ret:x/y", f"ret:{pid}", f"ret:{pid}/{u.upper()}", f"x:{pid}/{u}"):
        assert not fmt.is_valid_address(bad)
    assert fmt.is_valid_address(fmt.new_address(pid))


def test_uuid7_default_is_valid_and_time_ordered():
    a, b = fmt.uuid7(1000), fmt.uuid7(2000)
    assert fmt.is_uuid7(a) and fmt.is_uuid7(b) and a < b
    with pytest.raises(ValueError):
        fmt.uuid7(1, b"short")


def test_format_created():
    assert fmt.format_created(1796000000123) == "2026-11-30T00:53:20.123Z"
    assert fmt.parse_created("2026-11-30T00:53:20.123Z").microsecond == 123000
    with pytest.raises(ValueError):
        fmt.parse_created("2026-11-30T00:53:20Z")
