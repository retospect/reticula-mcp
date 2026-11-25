# Reticula MCP

Reticula MCP is a server that gives an AI assistant a long-term memory it can share. The assistant saves small notes through MCP tools. Notes are filed under hubs, and a hub can be shared with other people who have an account on the same server. Every note has a permanent global address and an Ed25519 signature from the moment it is written. The server is written in Python and stores everything in PostgreSQL with pgvector.

Reticula MCP is not related to the Reticulum mesh network and does not run on it.

## Status

This describes phase one. Version zero of the note format may still change.

What phase one is:

- One server instance with one PostgreSQL database and any number of accounts.
- Notes (called atoms), hubs, and sharing of hubs between accounts on that instance.
- Search over everything an account may read, by keywords and by meaning.
- Signed notes: addresses and signatures are in place so that federation can be added later without rewriting notes.

What phase one is not:

- No federation. Two instances do not exchange notes yet.
- No encryption. The format has room for it; nothing uses it.
- No web interface except the sign-in page that the assistant's connector opens.
- No messaging, review workflow, paper management or installer.

## Quick start: run your own server

You need Python 3.12 or newer with [uv](https://docs.astral.sh/uv/), and a PostgreSQL database with the pgvector extension. The tests run on PostgreSQL 17; other recent versions are untested. The database role that runs the first migration must be allowed to create the extensions `vector`, `citext` and `pg_trgm`. The `postgres` superuser in the image below can.

1. Start a database. Save this as `compose.yaml`, change the password, and run `docker compose up -d`:

   ```yaml
   services:
     db:
       image: pgvector/pgvector:pg17
       restart: unless-stopped
       environment:
         POSTGRES_PASSWORD: change-me
         POSTGRES_DB: reticula
       ports:
         - "127.0.0.1:5432:5432"
       volumes:
         - reticula-db:/var/lib/postgresql/data
   volumes:
     reticula-db:
   ```

2. Tell the server where the database is:

   ```
   export RETICULA_DATABASE_URL=postgresql://postgres:change-me@127.0.0.1:5432/reticula
   ```

3. Create the tables and the shipped instructions, then start the server:

   ```
   uvx reticula-mcp migrate
   uvx reticula-mcp serve
   ```

   `serve --migrate` does both in one step. The server listens on `127.0.0.1:8787`. The MCP endpoint is `/mcp` and the health check is `/healthz`.

4. Check it: `curl http://127.0.0.1:8787/healthz` answers `{"status":"ok",...}`.

### Database and server together

The repository has a compose file that runs the database and a released server from PyPI side by side: [deploy/compose.yaml](https://github.com/retospect/reticula-mcp/blob/main/deploy/compose.yaml). Copy `deploy/.env.example` to `deploy/.env`, set `RETICULA_DB_PASSWORD` and `RETICULA_VERSION`, and run `docker compose up -d --build` in `deploy/`. The server then listens on `127.0.0.1:8787`.

### Settings

All settings are environment variables. There are no config files.

| Variable | Default | Meaning |
|---|---|---|
| `RETICULA_DATABASE_URL` | none, required | PostgreSQL connection URL. |
| `RETICULA_PUBLIC_URL` | none | The base URL clients use, without a trailing slash. Set it whenever clients reach the server under any name other than localhost. It is used for the sign-in flow and added to the allowed Host and Origin headers. The URL you give an assistant is this value plus `/mcp`. It must start with `https://` unless the host is `localhost` or `127.0.0.1`. |
| `RETICULA_HOST` | `127.0.0.1` | Address to listen on. `serve --host` overrides it. |
| `RETICULA_PORT` | `8787` | Port to listen on. `serve --port` overrides it. |
| `RETICULA_SIGNUP` | `open` | `open` or `closed`. `closed` stops new sign-ups on the sign-in page. Accounts can still be created with the command line. |
| `RETICULA_SIGNUP_RATE` | `5/hour` | Sign-up attempts allowed per client IP address. Units: second, minute, hour, day. |
| `RETICULA_REGISTER_RATE` | `20/hour` | Connector registrations allowed per client IP address. Each assistant registers once when it is connected. |
| `RETICULA_TRUSTED_PROXIES` | empty | Comma-separated IP addresses or CIDR ranges whose `X-Forwarded-For` header is believed. Empty trusts nobody. Set it to your reverse proxy's address, or every sign-up counts as coming from the proxy. |
| `RETICULA_KNOWN_CLIENTS` | `claude.ai,claude.com` | Comma-separated hosts that an assistant's connector returns to after sign-in. The sign-in page warns when a connection would hand access to any other host. Connections that return to this computer never warn. |
| `RETICULA_EMBEDDER` | `auto` | `auto`, `hash` or `fastembed`. `fastembed` runs a local model and needs the extra `reticula-mcp[fastembed]`. `hash` needs nothing and has no real sense of meaning; keyword matching still works. `auto` uses `fastembed` when it is installed and `hash` otherwise. |
| `RETICULA_EMBED_MODEL` | model default | Model name for the `fastembed` embedder. |
| `RETICULA_STATELESS_HTTP` | `true` | Keep no MCP session state on the server, so a restart does not strand a connected assistant. |

To get the local embedding model, run `uvx --from 'reticula-mcp[fastembed]' reticula-mcp serve`.

### Reaching the server from your assistant

The server speaks streamable HTTP and does not terminate TLS itself. Put it behind something that does when it leaves your machine.

The setup this project documents is [Tailscale](https://tailscale.com/). On a tailnet you run the server on one machine and reach it from your other machines by its tailnet name. Traffic between tailnet machines is encrypted, so plain `http://` to a tailnet address is acceptable for assistants that run on your own machines. For `https://` on your tailnet name, run `tailscale serve` in front of port 8787, and set `RETICULA_PUBLIC_URL` to the name it prints.

A cloud-hosted assistant makes the request from the provider's servers. Those are not on your tailnet. For them the server must be reachable on the public internet over HTTPS. Use Tailscale Funnel (`tailscale funnel`), or any reverse proxy or tunnel that terminates TLS and forwards to port 8787. Set `RETICULA_PUBLIC_URL` to the public name, and set `RETICULA_TRUSTED_PROXIES` to the proxy's address. The flags of the `tailscale` command differ between versions: read `tailscale serve --help` and `tailscale funnel --help` for yours. This project has not tested every assistant product against these setups, so check the connector with your own client. Any other way of making the URL reachable works as well.

## Connecting an assistant

1. In the assistant's settings, add a custom connector (a remote MCP server) and paste the URL: your `RETICULA_PUBLIC_URL` plus `/mcp`, for example `https://reticula.example.net/mcp`. On the machine that runs the server, `http://127.0.0.1:8787/mcp` works.
2. The assistant opens the sign-in page. Sign in, or create an account with an e-mail address and a password. The page sends you back to the assistant.
3. Ask the assistant to call `start`. It receives who it is, a starting instruction and your hubs, and then saves its first note.

Each connected assistant gets its own token, which can be revoked on its own.

A local client that cannot do the sign-in flow can use a token minted on the server:

```
reticula-mcp account create --email me@example.com
reticula-mcp token --email me@example.com --label laptop
```

`account create` asks for a password (add `--handle name` to choose the handle). `token` prints a bearer token once; only a hash is stored. `--ttl-days N` makes it expire. Send it in the header `Authorization: Bearer <token>`. For example, with Claude Code:

```
claude mcp add --transport http reticula http://127.0.0.1:8787/mcp --header "Authorization: Bearer <token>"
```

## The tools

| Tool | What it does |
|---|---|
| `start` | The first call in a conversation. Returns your handle and person ID, the starting instruction, and your hubs. |
| `profile` | Shows your account, or renames your handle. The person ID never changes. |
| `search` | One ranked search over notes, hubs, instructions and paper stubs that you may read. |
| `get` | Reads a note or a hub with its surroundings. A hub shows its newest members first. `history` adds earlier versions. |
| `put` | Writes a note, a hub, a paper stub, an instruction or a debug note and returns its address. |
| `edit` | Makes a new version of one of your own notes. The address stays. |
| `link` | Files a note under a hub, withdraws it again, or adds another relation. |
| `delete` | Deletes one of your own notes. The address stays and answers as deleted. |
| `share` | Gives an account access to a hub, changes it, revokes it, or lists who has it. |

A note is addressed as `ret:<person id>/<uuid>`. Wherever a tool takes a `ref`, the last six or more characters of the note's id also work if they are unique among the notes you can read.

## How sharing works

- A hub is a note of kind `hub`. Notes filed under it belong to it. Hubs can be filed under other hubs.
- `share(hub, person, mode)` gives another account on the same instance access. `person` is `@handle` or a person ID. `mode` is `add` (read and add), `read` or `none` (revoke).
- Access to a hub covers the hubs under it.
- There is no publish step. A note filed under a shared hub is readable by every member at once. `private=true` keeps a note to its author, even inside a shared hub.
- You cannot edit someone else's note. You write your own with `supersedes`, and the old note stays readable by its address.

Hub presets: `group` (members read and add, any member may invite), `moderated` (members read and add, only the hub's owner invites), `personal` (only the owner adds, others read).

## What is signed, and why

Every account gets three key slots when it is created: a root key, a working key and an encryption key slot that is not used yet. The person ID is derived from the root public key and never changes, not when the handle changes and not when keys are replaced. Every note, hub membership, grant and deletion is a record with a global address and an Ed25519 signature over its exact bytes. Records are appended to a log and never rewritten, except that the content of a deleted note is erased while its hash and address stay.

The reason is federation. Because each record already carries its author, address and signature, another instance can later verify a note it receives without trusting the instance that sent it, and no stored note needs to be rewritten for that. The format is specified in [docs/format.md](https://github.com/retospect/reticula-mcp/blob/main/docs/format.md), with test vectors in `tests/vectors/v0.json`.

## Security notes

- The instance that hosts your account holds your private keys, unencrypted, in its database. The signature shows which account wrote a note and that it has not changed since. It does not protect you from the operator of your instance.
- The application does not encrypt anything at rest. Anyone who can read the database can read every note. Use disk encryption and database access control if that matters to you.
- Passwords are stored as argon2id hashes. Bearer tokens are stored as SHA-256 hashes.
- Sign-ups are open by default and rate-limited per IP address. Set `RETICULA_SIGNUP=closed` to close them.
- Serve the MCP endpoint over HTTPS when it leaves your machine or tailnet. A bearer token sent over plain HTTP can be read on the way.
- Text found in the graph is treated as data written by people and other assistants, never as instructions to follow. The only exception is an instruction note written by the instance's own system account, which is where the shipped instructions come from.
- Assistants can write debug notes about tool problems. They go to a hub that every account may add to and only the operator reads.
- Backups are your job. One line: `pg_dump --format=custom "$RETICULA_DATABASE_URL" > reticula.dump`. The dump contains the private keys, so protect it like the database. [docs/operating.md](https://github.com/retospect/reticula-mcp/blob/main/docs/operating.md) covers upgrades, backups and restores, tokens and sign-ups.

## Development

```
uv sync
scripts/test
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

`scripts/test` starts a throwaway `pgvector/pgvector:pg17` container with docker and runs the whole suite against it. Tests use a real PostgreSQL. To use your own server instead, set `RETICULA_TEST_DATABASE_URL` to a superuser URL on a server that has pgvector.

[AGENTS.md](https://github.com/retospect/reticula-mcp/blob/main/AGENTS.md) describes the code layout, the frozen interfaces and the conventions. [docs/format.md](https://github.com/retospect/reticula-mcp/blob/main/docs/format.md) is the normative note format.

Licence: AGPL-3.0-or-later. See `LICENSE`.
