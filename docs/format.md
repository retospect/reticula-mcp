# Reticula note format, version 0

Normative. The words MUST, SHOULD and MAY have their RFC 2119 meaning. The
reference implementation is `src/reticula_mcp/format/`; the conformance kit is
`tests/vectors/v0.json` (self-describing; every expected value there MUST be
reproduced bit for bit) with an independent second implementation in
`tests/reference.py`. If this text and the vectors disagree, the vectors win and
this text is a bug.

Format v0 is FROZEN. Changes go in a new format version, never in v0.

## 1. Conventions

* `b64u(x)`: base64url (RFC 4648 section 5), no padding. Decoders MUST reject
  padding, foreign characters and non-canonical encodings.
* `b32(x)`: RFC 4648 base32, lowercase, no padding.
* `sha256(x)`: SHA-256. `hex(x)`: lowercase hexadecimal.
* **Canonical JSON** (what producers emit): UTF-8; object keys sorted by Unicode
  code point; separators `,` and `:` with no whitespace; non-ASCII characters
  written raw (not `\u`-escaped); control characters escaped as `\b \t \n \f \r`
  or `\u00xx` (lowercase hex); **no floats**, no NaN or infinities (integers,
  strings, booleans, null, arrays, objects only). For float-free documents this
  is intended to coincide with RFC 8785 (JCS); that is not claimed.
* All text is Unicode; a string containing a lone surrogate is invalid.

## 2. Identifiers

* **Key ID**: a short name of a key slot inside one account: `root`, `k1`, `e1`
  in v0.
* **Person ID**: `"p" + b32(sha256(b"reticula/person/v0\x00" + root_public_key))[:26]`
  where `root_public_key` is the raw 32-byte Ed25519 public key. Derived once at
  sign-up; permanent. It does not change when keys rotate or the handle changes.
* **Node UUID**: UUIDv7 (RFC 9562), lowercase, hyphenated: 48-bit big-endian Unix
  milliseconds, version nibble 7, 12 random bits, variant `10`, 62 random bits.
* **Address**: `ret:<person_id>/<uuid>`. Fixed at creation, never changes, never
  reused. Every version of a node shares its address.
* **Hub ID** (opaque): `"h" + b32(sha256(b"reticula/hub/v0\x00" + utf8(hub_address)))[:26]`,
  where `hub_address` is the address of the hub atom. Used so that records filed
  under a hub reveal nothing about the hub beyond a stable pseudonym.
* **Version hash**: `"sha256:" + hex(sha256(envelope_bytes))`. Because `parents`
  is inside the envelope, a version hash covers its whole ancestry.
* **Content hash**: `"sha256:" + hex(sha256(salt_bytes + payload_bytes))`, where
  `payload_bytes` is the UTF-8 encoding of the PLAINTEXT payload and
  `salt_bytes` is empty while `salt` is `"none"`. While `enc` is `"none"` the
  plaintext payload is the envelope's `payload` string itself. Once hubs are
  encrypted the hash stays a hash of the plaintext, salted from the hub key, so
  members can match identical content and outsiders cannot test a guess; v0
  defines no such scheme and rejects any `enc` other than `"none"`.

## 3. The envelope

A record is ONE byte string, the **envelope**, plus a signature over those
bytes. Producers write the envelope as canonical JSON with these fields:

| field          | type            | meaning |
|----------------|-----------------|---------|
| `v`            | integer         | format version, `0` |
| `type`         | string          | `atom`, `member`, `grant`, `tombstone`, `identity`; other values are legal and MUST be tolerated on read |
| `address`      | string          | the node this record is a version of |
| `author`       | string          | person ID of the signer; in v0 MUST equal the person part of `address` |
| `key`          | string          | key ID of the signing slot |
| `created`      | string          | RFC 3339 UTC with exactly three fractional digits: `2026-11-25T07:14:38.123Z` |
| `parents`      | array of string | version hashes of the previous version(s) of THIS address; `[]` for the first version |
| `hub`          | string or null  | opaque hub ID the record is filed under |
| `audience`     | string          | `hub` (inherit the hub's audience), `private` (author only), `public` (anyone; used by `identity`); other strings tolerated |
| `alg`          | object          | `{"hash":"sha256","sig":"ed25519","enc":"none"}` |
| `salt`         | string          | salt scheme; `"none"` in v0 (a hub-key-derived salt arrives with encryption) |
| `content_hash` | string          | see section 2 |
| `payload`      | string          | the payload's own JSON text (so the payload bytes are unambiguous). When `enc` is not `none` this becomes base64url ciphertext |

### 3.1 Never re-serialise

Hashes and signatures are over the envelope bytes exactly as they were produced.
A consumer MUST NOT parse and re-serialise an envelope to hash, verify, store or
forward it. Records with unknown fields, unknown `type`, unknown `audience`
values or unknown payload kinds MUST be stored and re-served byte-identically
and, if the algorithms are known, still verify. A consumer MAY refuse records it
cannot verify (unknown `alg` values, `v` other than 0).

### 3.2 Signature

Ed25519 (RFC 8032) over the envelope bytes; 64 raw bytes. The **text form** of a
signed record, used for export, is `b64u(envelope) + "." + b64u(signature)`.

### 3.3 Verification

A record is valid when: the envelope parses as a JSON object; the defined fields
have the types above; `author` equals the person part of `address`; `alg` is a
supported combination; `content_hash` matches `payload`; the signing key `key`
belongs to `author`; the signature verifies under that key's public key.
Type-specific key rule: `identity` records are signed by the account's `root`
key; every other defined type by its delegate signing key (`k1`).

## 4. Keys

Each account has, from sign-up: `root` (Ed25519, use `sign`, role `root`), `k1`
(Ed25519, use `sign`, role `delegate`: the hosting instance's working key) and
`e1` (X25519, use `encrypt`: present, unused in v0). Algorithm names are carried
with every key so they can be replaced. Public keys appear in the identity
record as `b64u(raw public key)`. Key storage is an implementation matter (the
reference instance stores private keys unencrypted by the owner's decision).

## 5. Payloads

Payloads are JSON objects, serialised canonically by this implementation.
Consumers MUST ignore unknown members.

* **atom**: `{"kind": "note"|"hub"|"paper"|"instruction"|"debug"|<other>, "text": string,
  "title"?: string, "tags"?: [string], "supersedes"?: [address],
  "links"?: [{"rel": string, "to": address}],
  "ext"?: {"doi"?: string, "arxiv"?: string, "key"?: string}, "anchor"?: string,
  "origin"?: {"by": "person"|"agent", "model"?: string}}`.
  Hub atoms (`kind: "hub"`) MAY carry `"preset": "group"|"personal"`,
  `"invite": "members"|"admins"`, `"accept": "members"|"anyone"`.
* **member** ("this atom belongs to that hub"): `{"atom": address, "hub": address, "withdrawn": bool}`.
  A withdrawal is a new version of the same member address with `withdrawn: true`.
* **grant**: `{"hub": address, "to": person_id, "mode": "add"|"read"|"none", "wrapped_key": null}`.
  Revocation is mode `none`. `wrapped_key` is reserved for encryption.
* **tombstone**: a terminal version OF THE TOMBSTONED ADDRESS (`parents` = its
  head); payload `{"derived": bool}`. No version may follow it.
* **identity**: `{"handle": string, "keys": [{"id", "use": "sign"|"encrypt",
  "role"?: "root"|"delegate", "alg", "pub": b64u}], "verified": {"value": bool,
  "at": string|null}}`; `audience` is `public`. Signed by the root key. Its address
  is fixed per account; handle or verification changes are new versions.

## 6. Versions and history

A node is the set of records sharing an address, forming a hash chain through
`parents`. The head is the latest version by the log order of the hosting
instance (v0 does not define a merge rule for forked histories; instances
SHOULD keep the newest accepted version as head). Content of a tombstoned
address MAY be erased by the host after the tombstone is written; the record's
hash, address, parents, author and time remain so chains stay visible, but the
erased versions can no longer be served or verified.

## 7. Test vectors

`tests/vectors/v0.json` contains: public test seeds and keys; person IDs; UUIDv7
cases (including all-zero and all-one random bytes); hub IDs; canonical-JSON
cases; one signed record of each type; a second version with a parent;
non-ASCII text; an unknown-type record with unknown fields; and verification
failures (flipped envelope byte, flipped signature byte, wrong key, malformed
text). The seeds are public and MUST NOT be used for real keys.
