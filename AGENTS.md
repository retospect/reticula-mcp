# AGENTS.md: Reticula MCP, entry point for lane agents

## What this is

* A shared memory graph for people and their assistants: small signed notes (atoms) filed under hubs; hubs can be shared with other people.
* Phase one: n accounts on ONE instance, ONE Postgres database. Sharing is a permission check on read. Long-term it federates (signed feeds); the wire format is already complete.
* Every account gets key pairs at sign-up (held by the instance). Every note gets a global address and a signature when written. All format fields exist and are stored even where nothing enforces them yet.
* The assistant reaches everything through MCP tools over streamable HTTP. Sign-up is e-mail + password inside the MCP connector's OAuth flow (sign-in lane).
* Not in phase one: encryption, services, messages, review gates, paper management, installer.

Normative format: `docs/format.md`. Conformance vectors: `tests/vectors/v0.json`.

## Set up, test, run

```
uv sync                               # python 3.13 (.python-version); requires-python >=3.12
scripts/test                          # whole suite against a throwaway pgvector Postgres (docker)
scripts/test tests/test_x.py -k name -x     # any pytest args
uv run ruff check . && uv run ruff format --check . && uv run mypy
export RETICULA_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/reticula
uv run reticula-mcp migrate           # also creates the system account, runs content.hooks.on_migrated
uv run reticula-mcp account create --email me@example.com    # prompts for a password
uv run reticula-mcp token --email me@example.com --label laptop   # prints a bearer token
uv run reticula-mcp serve [--migrate] [--host H] [--port P]  # default 127.0.0.1:8787, MCP at /mcp, health at /healthz
```

`scripts/test` starts `pgvector/pgvector:pg17` on a random localhost port, sets `RETICULA_TEST_DATABASE_URL`, removes the container afterwards. If that variable is already set (CI service container) nothing is started. Tests use a REAL Postgres, never mocks.

Environment: `RETICULA_DATABASE_URL`, `RETICULA_PUBLIC_URL` (external base URL, no trailing slash; OAuth issuer/resource later; also added to the allowed Host/Origin list), `RETICULA_HOST` (127.0.0.1), `RETICULA_PORT` (8787), `RETICULA_EMBEDDER` (`auto|hash|fastembed`), `RETICULA_SIGNUP` (`open|closed`), `RETICULA_SIGNUP_RATE` (`5/hour` per IP), `RETICULA_TRUSTED_PROXIES` (comma-separated IPs/CIDRs whose `X-Forwarded-For` is believed; empty = trust nobody; `Settings.trusted_proxies`), `RETICULA_EMBED_MODEL` (`Settings.embed_model`), `RETICULA_STATELESS_HTTP` (default `true`: no server-side MCP sessions). Parsed in `config.py` (`Settings.from_env()`, `parse_rate`).

## Layout and ownership

Edit ONLY what your lane owns. Everything not listed as owned is frozen (see below).

```
src/reticula_mcp/
  format/            FROZEN   address.py (ids, uuid7), envelope.py (canonical bytes, hashes), signing.py (ed25519, text form), encoding.py
  db/                FROZEN   pool.py (Database, connect, configure_connection), migrate.py; migrations/0001-0099 frozen
  db/migrations/NNNN_name.sql   each lane adds files in its own number range
  accounts.py        FROZEN   Account, create_account, authenticate, set_handle, set_verified, ensure_system_account, mint_token, lookup_token, account_for_token
  records.py         FROZEN   Record, sign_and_append, append_signed, get_record, head, versions, feed, check_record, verify_record
  permissions.py     signatures FROZEN   readable_nodes_sql, current_nodes_sql, can_read, can_add; the GRAPH lane replaces only the body of can_add
  resolve.py         FROZEN   resolve_ref, resolve_person
  context.py seams.py errors.py config.py cli.py server.py    FROZEN
  auth/              SIGN-IN lane   build_auth(settings, db) -> AuthWiring; foundation ships a bearer-token stub
  graph/             GRAPH lane     handlers.py (put edit link delete share), node materialisation, grants, readable_nodes body
  search/            SEARCH lane    handlers.py (search get), embedder.py, hooks.py (on_node_changed), embedding table
  content/           CONTENT lane   handlers.py (start profile), hooks.py (on_account_created, on_migrated), shipped instructions, README
tests/               conftest.py (fixtures), helpers.py, vectors/v0.json, contract/tools.json, test_*.py
                     each lane adds its own test_<lane>_*.py; never edit another lane's or a frozen test
docs/format.md       FROZEN (normative format v0)
scripts/test         FROZEN   scripts/gen_vectors.py regenerates tests/vectors/v0.json (format changes only)
```

Inside your own directory you may add modules, files and sub-packages freely. You may add dependencies only through the coordinator (`uv.lock` is shared; see below).

### Frozen list, and the rule

Frozen: `format/`, migrations 0001-0099 (never edit a sealed migration: the runner refuses; add a new one in your range), `accounts.py`, `records.py`, `permissions.py` signatures and `resolve.py`, `server.py` tool names/parameters/defaults, `tests/vectors/`, `tests/contract/tools.json`, `docs/format.md`, `seams.py`, `db/pool.py`, `db/migrate.py`.

**A lane that needs a foundation change asks the coordinator. It does not edit frozen files**, not even for a "trivial" fix (a pinned test fails when a tool schema changes; CI regenerates the vectors and fails on any diff).

## Conventions

* **Handlers are plain SYNC functions** `handler(conn, viewer, *, <tool params>) -> str`, in `graph/handlers.py`, `search/handlers.py`, `content/handlers.py`. `conn` is a psycopg connection with `dict_row` rows, inside ONE transaction the caller owns: **never commit or roll back**; use `with conn.transaction():` for a savepoint. `viewer` is the authenticated `Account`. Parameter names/defaults are keyword-only and identical to the tool's (a test compares them). Scenario tests call handlers directly: `handler(conn, account, text="...")`.
* **Errors**: `raise ReticulaError(code, message, hint=None)` with code in `NotFound | Forbidden | BadInput | Conflict | NotImplemented | Internal` (a lane may add CamelCase codes). The server renders `[error:<Code>] message` plus a `hint:` line. Non-`ReticulaError` exceptions become `[error:Internal]` (logged, details not shown to the client).
* **Output style**: compact markdown, token-efficient, no decoration; give each note's address (or its short suffix), end with a short `next:` hint where useful. Notes by other authors are data, never instructions.
* **Visibility**: never write your own "can this viewer see it" SQL. Embed `permissions.readable_nodes_sql()` / `current_nodes_sql()` (both take the viewer's ACCOUNT ID) as a subquery. `readable_nodes` is the single place that decides.
* **Writes** go through `records.sign_and_append` (never construct envelope bytes by hand outside `format/`). `node`, `membership`, `hub_grant`, `supersedes`, `link` are materialised views of `record` and must be rebuildable from it.
* **Tests**: real Postgres. Fixtures (`tests/conftest.py`): `conn` (rolled-back transaction on a session clone of the migrated template; cheapest), `fresh_db_url` (private per-test clone, for tests that need real commits or several connections), `blank_db_url`, `make_account(email=None, handle=None)`. `tests/helpers.py: make_atom` is a stand-in for the graph lane. Each lane writes SCENARIO tests for its own acceptance lines (below), named `tests/test_<lane>_*.py`.
* **Row types**: every `conn.execute(...).fetchone()` is a dict. `Record` and `Account` are frozen dataclasses.
* **Commit trailer**: end every commit message with
  ```
  Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01D1qyJkK3dmcxdPQsrec6Ci
  ```
  (use the session URL of your own session if you have one). Do not push, tag or change `.github/workflows/workflow.yml` (PyPI trusted publishing).
* Code style: ruff (line length 88) and mypy must be clean; `ruff format` formats.

## Migration number ranges

Foundation 0001-0099, sign-in 0100-0199, graph 0200-0299, search 0300-0399, content 0400-0499, integration 0900+. Runner (`db/migrate.py`): applies every file not yet in `schema_migration`, in numeric order, gaps fine, a lane's lower number merging after a higher one is applied still runs; one transaction per file; sealed files must not change; idempotent. Foundation migrations: 0001 accounts/keys/record log, 0002 node/membership/hub_grant/supersedes/link, 0003 embedding (search owns), 0004 OAuth + signup_attempt (sign-in owns, may reshape), 0005 `readable_nodes` stub and `current_nodes`.

## Seams (exact shapes)

1. **Auth seam** (`seams.py`, `auth/__init__.py`). `server.py` calls `auth.build_auth(settings: Settings, db: Database) -> AuthWiring` once and never looks inside. `AuthWiring(settings: AuthSettings|None, token_verifier: TokenVerifier|None, provider: OAuthAuthorizationServerProvider|None, routes: Sequence[BaseRoute], middleware: Sequence[starlette Middleware])`. `server.py` passes `settings`/`token_verifier`/`provider` to `FastMCP(auth=..., token_verifier=..., auth_server_provider=...)`, prepends `routes` to the Starlette app, adds `middleware`. At least one of `token_verifier`, `provider`, `middleware` must be set. Verified against mcp 1.30.0 source (`fastmcp/server.py` `__init__`, `streamable_http_app`; `server/auth/{provider,routes,settings,middleware/bearer_auth}.py`):
   * resource-server only (the stub): `settings` + `token_verifier`; the SDK adds `BearerAuthBackend`, `RequireAuthMiddleware` (401 with `WWW-Authenticate` + `resource_metadata`) and `/.well-known/oauth-protected-resource/mcp`.
   * full authorization server: `settings` (with `client_registration_options`, `revocation_options`) + `provider`; the SDK then serves `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-authorization-server` via `create_auth_routes`, and verifies bearer tokens through the provider. `provider` and `token_verifier` are mutually exclusive in the SDK.
   * own middleware: leave both unset and supply `middleware` that sets `scope["user"]` to `AuthenticatedUser(AccessToken(...))` and rejects unauthenticated `/mcp` calls itself.
   * Your sign-in/sign-up page and any other HTML routes go in `routes`. FastMCP custom routes are unauthenticated.
   * **Identity contract (do not break):** whatever mechanism, the authenticated principal reaches tools as an `AccessToken` on `scope["user"]` whose `subject == str(account.id)` (decimal account row id). `context.viewer_for_token` turns it into an `Account` (disabled accounts refused). The SDK binds each streamable session to the credential that created it (`authorization_context`: client_id/issuer/subject), so keep `client_id` and `subject` stable per connection.
   * The stub sets `validate_token_resource=False` (tokens have no resource). If you issue RFC 8707 resource-bound tokens, set it True and put the resource on `AccessToken.resource`.
   * Tokens: `oauth_token.token_hash` = sha256 hex of the token, never the token. Reuse `accounts.hash_token`, `mint_token`, `lookup_token` (checks kind=access, revoked, expiry, disabled account, bumps `last_used_at`). One revocable token per connected assistant (`grant_id`, `label`).
   * Rate limiting of sign-ups (per IP, `RETICULA_SIGNUP_RATE`, table `signup_attempt`): precis-mcp has a reference pattern (`src/precis_web/auth.py` `FailureTracker`, `_addr_bucket` folds IPv6 to /64); copy, do not depend.
   * Password verification goes through `accounts.authenticate` (argon2id, timing-equalised for unknown e-mails). Sign-up through `accounts.create_account` (creates keys, handle, signed identity record, calls `content.hooks.on_account_created`). `set_verified` exists for the later e-mail verification step (designed in, switched off).
2. **Handler locations**: `graph.handlers` (put edit link delete share), `search.handlers` (search get), `content.handlers` (start profile). `server.py` looks the function up at call time; keep the names. Internals may live in other modules.
3. **`readable_nodes` replacement**: the graph lane ships `CREATE OR REPLACE FUNCTION readable_nodes(viewer bigint) RETURNS TABLE(node_id bigint)` in `db/migrations/02NN_*.sql`. Semantics are documented in `0005_visibility.sql` (own atoms; hub-audience atoms with a non-withdrawn membership in a hub where the viewer holds read/add directly or on an ancestor; hub atoms by the same rule; `private` author-only; tombstoned never). `current_nodes` needs no change. The graph lane also replaces the BODY of `permissions.can_add` (grants with mode `add` on the hub or an ancestor, `accept: anyone`, revocation); its signature stays.
4. **`on_account_created(conn, account)`** (`content/hooks.py`): called inside `create_account`'s transaction after account, keys and identity exist; not for the system account. **`on_migrated(conn)`**: called after `reticula-mcp migrate` / `serve --migrate`; must be idempotent (seed shipped instruction atoms signed by `accounts.ensure_system_account(conn)`).
5. **`on_node_changed(conn, node_id)`** (`search/hooks.py`): the graph lane calls it inside the writing transaction after every node insert/head change/tombstone; the search lane fills it (embedding upkeep). No-op today.
6. **Embedder interface** (`search/embedder.py`): `Embedder` protocol (`model: str`, `dim: int`, `embed(list[str]) -> list[list[float]]`), `HashEmbedder` (deterministic, dependency-free), `get_embedder(settings)`. The `embedding` table (`node_id, model, dim, vec vector, version_hash`, PK `(node_id, model)`) and the optional extra `reticula-mcp[fastembed]` belong to the search lane. pgvector adapters are registered on every connection when the extension exists (`db/pool.py`).
7. **Resolvers** (`resolve.py`): `resolve_ref(conn, viewer, ref, *, allow_tombstoned=False) -> node row (dict)`; ambiguity raises `Conflict` listing candidates. `resolve_person(conn, "@handle" | person_id) -> Account`.
8. **Erasure path**: `record` is append-only (trigger). The ONLY exception is SQL `blank_record_content(record_id)`, which empties envelope and signature of one non-tombstone version of an address that already has a tombstone record (keeps hash, address, parents, times). The graph lane decides when to call it (the `derived` flag etc.); `records.check_record` reports blanked versions as unverifiable.

9. **CLI subcommands**: `cli.py` is frozen, but each lane may add `src/reticula_mcp/<lane>/cli.py` with `register(sub)`; it adds parsers to the argparse subparsers object and gives each `set_defaults(func=run)` where `run(settings: Settings, args: argparse.Namespace) -> int`. `cli.py` imports `auth.cli`, `graph.cli`, `search.cli`, `content.cli` when they exist. Top-level command names must not collide: sign-in uses `connections`, search uses `reindex`, content uses `debug-notes`, graph uses `rebuild`.
10. **Graph API for other lanes** (`graph/api.py`, built by the graph lane; the content lane's bootstrap is bound to it at integration, so these names are fixed): `create_atom(conn, author, *, kind, text, title=None, tags=None, hub_node_id=None, private=False, supersedes=(), ext_id=None, anchor=None, model=None, payload_extra=None) -> dict` (node row), `update_atom(conn, author, node_id, *, text=None, title=None, tags=None, private=None, payload_extra=None) -> dict`, `add_membership(conn, actor, node_id, hub_node_id) -> None`, `set_grant(conn, actor, hub_node_id, grantee_person_id, mode) -> None`. Handlers are thin wrappers over this API.

Coordinator decisions (25 Nov 2026):
* Hub presets at the tool level are `group` (payload `preset: group`, `invite: members`), `moderated` (`preset: group`, `invite: admins`) and `personal` (`preset: personal`). `accept: anyone` is set only through `payload_extra` (the instance debug hub).
* Deleting an atom tombstones it AND blanks the content of every earlier version (`blank_record_content`); blanked versions are unverifiable by design, their hashes stay.
* Edits are serialised: a new version's `parents` is exactly `[head]` (take a row lock on the node). Forks arrive with a second device or instance.
* Identity records carry `audience: "public"`.
* `node.author_account_id` is set from the `account` table by person ID, also on rebuild.
* `create_account` raising `Conflict` for a known e-mail is accepted in "create account" mode (sign-ups are rate-limited); the sign-in mode must not reveal whether an e-mail exists.
* Filing: anyone who can read a note and can add to a hub may file it there (their own signed statement). Only a statement by the note's own author lets hub members read it; a statement by anyone else organises and confers nothing. Filing a hub under a hub stays author-only, because it decides grant inheritance.
* Who sees a hub's access list: the hub's admin and people with `add` access see every entry. A viewer with only `read` access sees the owner and their own entry, and no count of the others (`get`, `share`, `start`, `profile`). Reason: every account reads the shipped instructions hub, so the full list would be a list of all accounts.
* `put(kind="debug")` with no hub files itself into the system account's debug hub. Ordinary accounts cannot read that hub.
* The sign-in page states the host that receives access as its main line and warns for hosts outside `RETICULA_KNOWN_CLIENTS` (loopback never warns). Client registration is open, so the client's self-given name proves nothing.
* A grant's `revoked_at` is authoritative: no token of a revoked grant is accepted, whatever the token row says.
* Corrections by other people: a newer atom hides the one it supersedes when both have the same author. With different authors it hides the original only when both are filed in a common hub, each by its own author, the original is neither a hub nor an instruction, and the original's author has not objected. The objection is `link(ref=<mine>, to=<theirs>, rel="disputes")`; removing the link withdraws it. (`supersession_hides`, called by `current_nodes` and by `get`; migrations 0202 and 0203.)
* Below a hub: an invitation into a sub-hub is refused when a hub above it has `invite: admins`, or when that hub's admin removed the person, unless the inviter is that admin. When an admin removes a person from a hub, the hubs that person owns and filed under it (at any depth) are detached in the same transaction. A hub's admin may withdraw statements in the hubs below it.
* A `personal` hub takes adds from its owner only, whatever a hub above it grants.
* What a person is shown about a hub follows what they can do there, not the raw grant: the mode word and the access list go through the SQL function `shown_access` (migration 0203); `access.shown_access` is its Python form. `hub_access` alone says `add` for a personal hub under a hub the person can add to, and once showed its readers to them.
* A note an admin removed from a hub can be filed there again only by an admin (`membership.moderated`).
* Text other accounts control is never printed raw: `kind` is `^[a-z][a-z0-9_-]{0,31}$`, `rel` is `^[A-Za-z0-9_.:-]{1,64}$`, title at most 200 characters, 20 tags of 50, anchor 500, model 100, ext_id 300, checked on write in `graph/api.py` and quoted again on read. `start` lists hubs owned by others under their own heading.
* Limits: the server's statements stop after 15 s (`[error:Unavailable]`), CLI commands have none; writes per account are limited by `RETICULA_WRITE_RATE` (`[error:RateLimited]`); a failed write does not count. `get` depth is capped at 2.
* A handle someone gave up stays reserved for that person (`released_handle`); to others it reads as taken.
* A lane that needs a new dependency adds it to `pyproject.toml`, runs `uv lock` in its worktree, and says so in its report; the coordinator re-locks at merge.

Other facts: `records.sign_and_append` enforces local-write rules (own address, first version has no parents, parents exist and share the address, nothing after a tombstone, identity signed by `root`, everything else by `k1`); `records.append_signed(conn, envelope, signature)` is the lower-level ingest path (also the federation entry later) and stores bytes verbatim. `records.head` = newest row by log id.

## The tools (frozen: names, parameters, defaults)

`start()`; `profile(handle=None)`; `search(query, hub=None, kind=None, limit=10, include_superseded=False)`; `get(ref, history=False, depth=1)`; `put(text="", kind="note", hub=None, title=None, tags=None, private=False, supersedes=None, ext_id=None, anchor=None, preset=None, model=None)`; `edit(ref, text=None, title=None, tags=None, private=None, preset=None)`; `link(ref, to, rel="part-of", remove=False)`; `delete(ref)`; `share(hub, person=None, mode="add")`. Schemas are pinned in `tests/contract/tools.json`. `ref`/`hub`/`to` accept a full address or a unique 6+ hex suffix of the UUID among readable nodes; `person` accepts `@handle` or a person ID. Stubs return `[error:NotImplemented]` naming the owning lane.

Texts an assistant reads before it acts are tested, not reviewed. A change to the server's instructions, to a tool description or to the starter is tested before it is merged: scenario 3 of `docs/trials.md` on Haiku, side by side with the text it replaces, at least 12 runs each, counting `start` called first in every new conversation and the mode of the share. A change to a tool description or to the server's instructions is also run on scenarios 4, 18 and 22, where a conversation starts with one plain request and Haiku skips `start` most easily (`docs/trials.md`, round ten), and on those three `start` first is counted for Sonnet as well: a first sentence of the instructions that held for Haiku made Sonnet skip `start` in the email conversation of scenario 22 in 42 of 48 runs, against 13 of 48 before (round fourteen). All changed texts are tested together, as they will ship. The result is judged against the batch that ran at the same time, not against zero: the same texts gave 0 skipped of 156 in five batches and 7 of 144 in the next four. Two sentences that were each harmless alone made one model skip `start` in one conversation of five when both were added (24 of 114, against 7 of 300 without both; `docs/trials.md`, round seven). What a tool needs to teach goes into its reply (`next:` lines), which is read at the moment it matters.

No new tool parameter, for now. Every one tried in 0.3.0 (`part`, with and without a description, two forms of `from_hub`, `listed`, `word_handles`, `since` in eight of nine forms) made Haiku skip `start` in some first conversations of a client without the standing line (`docs/trials.md`, round eighteen, and its repeat on 0.3.0 as released); with the line in the client's instructions none cost anything measurable. So new behaviour goes into an existing parameter (a form of `ref`, as the lettered groups `<hub>:A` do; the settings ref `edit(ref="settings", ...)`) or into a reply. A new parameter ships only after the same trial shows its cost on clients without the line and Reto accepts that cost, which depends on how many connections have been onboarded.

`delete` works in two steps, the same for every client, through a form of `ref`: `delete(ref="<id>")` deletes nothing and names what would be deleted with the confirming call, `delete(ref="<id>:yes-<code>")` deletes (`graph/handlers.py`). The code is `accounts.confirmation_code`: four hex of an HMAC under a subkey of the server's secret (`keystore.confirmation_key`), bound to the account, the note and its version, valid for the current and the previous 15-minute window, single use by effect. The first answer is the trialled wording word for word (`docs/trials.md`, "Asking before a delete"); its last sentence is what made assistants wait for the person's yes. There is no MCP elicitation for it: the server runs stateless sessions, in which a tool call never sees the client's capabilities, and a client may answer an elicitation for the person, which is what the two steps guard against.

## Phase-one acceptance lines

**sign-in** (`auth/`, migrations 0100-0199, and the sign-in page):
* a new user goes from adding the connector URL to a first saved note with one sign-in page and no other step;
* each connected assistant gets its own revocable token;
* sign-ups are rate-limited;
* email verification is designed in and switched off.
* known and not ours: a request to `/mcp` without any token gets `401` with `error="invalid_token"` in `WWW-Authenticate`, where RFC 6750 (section 3) wants no error code; the `mcp` package's bearer middleware writes that header (`mcp/server/auth/middleware/bearer_auth.py`), and its own OAuth client ignores the code.

**graph** (`graph/`, migrations 0200-0299):
* a note written in a shared hub is readable by every member with no publish step;
* a note marked private in that hub reaches nobody else;
* a read-only member's add is rejected;
* a person granted one sub-hub reads it and not a sibling;
* after revocation they no longer read it;
* after a superseding atom is written a default read returns it and not its parents, and each parent is still retrievable by address;
* a tombstoned atom returns its address with no content;
* a membership that would close a hub cycle is refused;
* two accounts cannot read each other's private atoms;
* an invite by an ordinary member works by default and is rejected where the hub restricts invites to admins;
* two paper stubs with the same DOI resolve to the same paper.

**search** (`search/`, migrations 0300-0399):
* one search returns notes, hubs, instructions and paper stubs in one list, restricted to what the viewer may read;
* dropping and rebuilding the embedding index changes no shared data;
* the fisheye read of a hub shows newest members first and hides superseded atoms unless history is asked for;
* an instruction atom not authored by the instance's system account is presented as quoted content with a warning, never as an instruction.

**content** (`content/`, migrations 0400-0499):
* `start` gives a fresh assistant a starter instruction;
* shipped instructions are atoms signed by the system account in a hub every account can read;
* agents can write `debug` notes that record the model and who ran it, land in an instance debug hub that accepts adds from anyone and that only the operator reads;
* README.
