# Operating a Reticula MCP instance

This page is for the person who runs the server. Settings are listed in the [README](../README.md).

The commands are written for the Docker Compose files in [`deploy/`](../deploy) and are run from that directory. They were run as written here against a throwaway copy of the stack, with Docker Compose 5.8 on macOS, where Docker runs inside a Linux virtual machine. To run the server without Docker, see [Without Docker](#without-docker) at the end.

## Start

1. Create the settings file.

   ```
   cd deploy
   cp .env.example .env
   ```

   Set two values in `.env`:

   - `RETICULA_DB_PASSWORD`: generate one with `openssl rand -hex 24`.
   - `RETICULA_VERSION`: a release from PyPI.

   Leave `RETICULA_PUBLIC_URL` and `RETICULA_TRUSTED_PROXIES` empty for now.

2. Start the database and the server.

   ```
   docker compose up -d --build --wait
   ```

   To run a checkout instead of a release, add the override file. Use the same two `-f` options on every later `docker compose` command.

   ```
   docker compose -f compose.yaml -f compose.source.yaml up -d --build --wait
   ```

   The first build of a checkout took 30 seconds. With the image built, the command returned after 11 seconds, with both containers healthy. The server container waits for the database, applies pending migrations and then serves.

3. Check health.

   ```
   curl -s http://127.0.0.1:8787/healthz
   ```

   The answer is status 200 and `{"status":"ok","version":"0.1.0"}`, with the installed version. When the database does not answer, the status is 503. `docker compose ps` shows the same result as `healthy` or `unhealthy`.

4. Run the smoke test. It needs [uv](https://docs.astral.sh/uv/).

   ```
   ../scripts/smoke http://127.0.0.1:8787
   ```

   It walks the path an assistant takes: discovery, client registration, sign-up, token exchange, then `start`, `put`, `search`, `get` and `delete` over MCP, and it revokes its tokens at the end. It prints one line per step, 14 in all. Each must start with `ok`.

   Each run leaves one account named `smoke-<id>@example.com`, one registered client and one erased note. The last lines of the output list them. Each run also counts as one sign-up from your address, so with the default limit the sixth run within an hour fails at `sign-up` with status 429. To sign in to an account from an earlier run instead, pass `--email` and put its password in `SMOKE_PASSWORD`.

5. When a public name exists, see the next section.

## Put a public name in front

The server is published on `127.0.0.1` only and speaks plain HTTP. A tunnel or a reverse proxy on the same machine terminates TLS and forwards to `http://127.0.0.1:8787`.

1. Point the tunnel or proxy at `http://127.0.0.1:8787`.

2. Set two values in `.env`. The first is the address people use, with the port if it is not 443 and without a trailing slash. The second is explained in the next section.

   ```
   RETICULA_PUBLIC_URL=https://notes.example.com
   RETICULA_TRUSTED_PROXIES=172.31.88.1
   ```

3. Apply them. This recreates the server container only.

   ```
   docker compose up -d --wait
   ```

4. Run the smoke test against the public name.

   ```
   ../scripts/smoke https://notes.example.com
   ```

   All 14 lines must start with `ok`, and the `authorize` line must end with `cookie Secure`.

What changes once `RETICULA_PUBLIC_URL` is set:

- Every address the server hands out starts with the public URL: the OAuth metadata, the sign-in redirect and the `WWW-Authenticate` challenge. This holds for requests that arrive on the loopback port too. The smoke test against `http://127.0.0.1:8787` therefore fails at `discovery` from now on. `/healthz` on the loopback port keeps working.
- The sign-in cookie is marked `Secure` whenever the public URL starts with `https://`.
- Tokens issued at sign-in are bound to the public URL. After a change of the URL, every connected assistant gets status 401 and has to connect again. Tokens minted with `reticula-mcp token` are not bound and keep working.
- `/mcp` answers only requests whose `Host` header is the host and port of the public URL, or a loopback name such as `127.0.0.1:8787`. See the table below.

## A tunnel or proxy on the same host

The limits on sign-ups, client registrations and failed sign-ins are counted per client address. Behind a tunnel or proxy the server sees only the proxy's address, unless it is told which peer to believe. `RETICULA_TRUSTED_PROXIES` names the peers whose `X-Forwarded-For` header is believed.

| Setup | `RETICULA_TRUSTED_PROXIES` |
|---|---|
| The tunnel or proxy is a process on the host, forwards to `127.0.0.1:8787` and sets `X-Forwarded-For` | `172.31.88.1` |
| The proxy is a container in the compose network and sets `X-Forwarded-For` | the address of that container (give it a fixed one) |
| The tunnel forwards plain TCP, or sets no `X-Forwarded-For` | empty |

`172.31.88.1` is the gateway of the compose network. A process on the host that connects to the published port reaches the server from that address, not from `127.0.0.1`. `compose.yaml` fixes the subnet of its network to `172.31.88.0/24` so that this address stays the same.

Measured, with the gateway address trusted and requests sent from the host to the published port:

| `X-Forwarded-For` sent | Address the request was counted under |
|---|---|
| `203.0.113.7` | `203.0.113.7` |
| `198.51.100.9, 203.0.113.7` | `203.0.113.7` (the last address that is not a trusted proxy) |
| two header lines, `198.51.100.9` then `203.0.113.8` | `203.0.113.8` |
| `2001:db8:1:2:3:4:5:6` | `2001:db8:1:2::/64` (IPv6 clients are counted per /64) |
| none, or a value that is not an address | the gateway address |

Measured, with a Caddy container as the proxy and two clients behind it, and the sign-up limit set to 2 per hour:

- Proxy address trusted: each client was counted under its own address. The third sign-up of the first client got status 429. The second client could still sign up. A client that sent its own `X-Forwarded-For` through the proxy was still counted under its real address.
- Nothing trusted: both clients were counted under the proxy's address. After two sign-ups by the first client, the second client got status 429.

What follows from that:

- With `RETICULA_TRUSTED_PROXIES` empty, all clients behind the proxy share one bucket. A few sign-ups or failed sign-ins by anyone lock everyone out for the window. The server writes a warning to its log at start when the public URL is `https` and the setting is empty.
- Trust a peer only if it writes `X-Forwarded-For` itself, by replacing the header or by appending to it. If you trust a tunnel that passes the client's own header through, or that sets none, every client can choose the address it is counted under and the limits stop working. Leave the setting empty in that case.
- Trusting the gateway address trusts every process on the host that can reach the published port.

The proxy must leave the `Host` header as the client sent it. Measured on `/mcp` with a valid token and `RETICULA_PUBLIC_URL=https://notes.example.com`:

| `Host` header | Status |
|---|---|
| `notes.example.com` | 200 |
| `127.0.0.1:8787` | 200 |
| `notes.example.com:443` | 421 |
| `app:8787` (the name of the upstream) | 421 |
| any other name | 421 |

When the public URL carries a port, the `Host` header must carry it too. A request with an `Origin` header from another site gets status 403. The sign-in page, the OAuth endpoints and `/healthz` do not check `Host`. The addresses they return come from `RETICULA_PUBLIC_URL`, never from the request.

### Check the setting

First check which address the server sees for the host:

```
curl -s -o /dev/null http://127.0.0.1:8787/healthz
docker compose logs app | grep 'GET /healthz' | grep -v ' 127.0.0.1:' | tail -1
```

The address in front of the port in that line is the one to trust. Lines from `127.0.0.1` are the container's own health check.

Then check the whole path. Register a client through the public name, with a made-up `X-Forwarded-For`, and read the address it was counted under:

```
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://notes.example.com/register \
  -H 'Content-Type: application/json' -H 'X-Forwarded-For: 203.0.113.99' \
  -d '{"client_name":"proxy check","redirect_uris":["http://127.0.0.1:53682/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}'
docker compose exec -T db psql -U reticula -d reticula -c \
  "SELECT subject, created_at FROM auth_event WHERE kind = 'register_ip' ORDER BY created_at DESC LIMIT 3"
```

| Newest `subject` | Meaning |
|---|---|
| your own public address | correct |
| `203.0.113.99` | the tunnel passes client headers through: set `RETICULA_TRUSTED_PROXIES` back to empty |
| the gateway address | no header arrives: leave the setting empty, all clients share one bucket |

### Change the subnet

If `172.31.88.0/24` collides with a network you already have, set `RETICULA_SUBNET` in `.env`. The gateway is the first address of the subnet.

A change of the subnet does not apply cleanly to a running stack. Compose recreates the network, but the running containers lose their names on it: the server logs `waiting for the database` and `docker compose up --wait` does not return. This also happens on the first `up` after an update that introduced the fixed subnet. Take the stack down first. The volumes stay.

```
docker compose down
docker compose up -d --build --wait
```

Never add `-v` to `down`: it deletes the database. If the stack is already stuck, the same two commands repair it. It was back and healthy after 11 seconds with its data.

### Tailscale Funnel

- Funnel serves only the machine's own `ts.net` name, on ports 443, 8443 and 10000. It cannot serve a domain of your own.
- On a machine that already uses Funnel on 443, a second service goes on another port with `--https=<port>`. `RETICULA_PUBLIC_URL` must then carry that port.
- A port other than 443 works only for assistants that connect from your own machines, such as Claude Code. Claude's custom connectors (claude.ai, the desktop and mobile apps) connect from Anthropic's servers and accept port 443 only: "Claude can't connect to a URL that specifies another port" (Claude support, "Get started with custom connectors using remote MCP", read on 2026-11-25). For those, run Reticula on a machine whose port 443 is free, or put a reverse proxy with a name of its own in front.
- `tailscale funnel reset` removes every service of the machine, not one.
- In its HTTPS mode Funnel writes `X-Forwarded-For` itself and replaces a value sent by the client. Its TCP modes add no header. This is read from the Tailscale source, not measured here, so run the check above.

## Limits

| Limit | Setting or value |
|---|---|
| Sign-ups per client address | `RETICULA_SIGNUP_RATE`, default `5/hour` |
| Client registrations per client address | `RETICULA_REGISTER_RATE`, default `20/hour` |
| Failed sign-ins for one e-mail address from one client address | 6 in 15 minutes |
| Failed sign-ins from one client address | 30 in 15 minutes |
| Writes per account (`put`, `edit`, `link`, `share`, `delete`) | `RETICULA_WRITE_RATE`, default `600/hour` |
| Time for one database statement of a tool call | 15 seconds, then the tool answers `Unavailable` |
| Password length | at least 10 characters |

The limits per client address depend on `RETICULA_TRUSTED_PROXIES`, as described above. Over the write limit a tool answers `RateLimited` and says when to try again. Expired tokens and authorization codes are deleted 3 days after they expire.

## Logs

```
docker compose logs app
```

At the default log level the server writes one line per request: the peer address, the method, the path without its query string, and the status. Behind a proxy the peer address is the proxy's. After a full smoke run and a failed sign-in, the log was searched for the password, the tokens, the authorization code, the e-mail address, the handle and the note text. None of them was in it.

## Upgrade

1. Back up the database (below).
2. Set the new `RETICULA_VERSION` in `.env`. For a checkout, update the checkout.
3. Rebuild and restart.

   ```
   docker compose up -d --build --wait
   ```

4. Check `/healthz` and run the smoke test.

The server container applies pending migrations when it starts and logs one line per file, for example `applied 0101_oauth_indexes.sql`. In the drill the command took 26 seconds. Tokens issued before the upgrade worked after it, and notes written before it read back unchanged.

Migrations only move forward and each file runs in its own transaction. A migration file that has already been applied is never edited; changes arrive as new files. If the server reports that an applied file has changed, you are running mismatched code and database: do not continue. To go back to the old version, restore the dump from step 1 as described under Restore, with the old `RETICULA_VERSION`.

The version-zero note format may still change. Read the release notes before you upgrade across a format change.

If the search index needs rebuilding after an upgrade, run `docker compose exec app reticula-mcp reindex`. The index holds only derived data, so rebuilding it changes no notes.

## Back up

The whole instance is one PostgreSQL database. Dump it while the server runs:

```
docker compose exec -T db pg_dump -U reticula --format=custom reticula > reticula-$(date +%Y%m%d).dump
```

The dump contains every note, the password hashes and the private keys of every account, unencrypted. Store it as carefully as the database itself, and off the machine that runs the database. Run the command from cron or a systemd timer. Keep a copy of `.env` with it: the dump does not contain the settings.

The embedding model is not in the dump. It is downloaded again when needed.

## Restore

Restore into an empty database. The steps below replace the data of an existing stack. On a new machine, start at step 2 with the same `.env`.

1. Stop the stack and remove the database volume. This deletes the current data.

   ```
   docker compose down
   docker volume rm reticula_reticula-db
   ```

2. Start the database only. If the server starts first, it creates its tables and the restore fails on them.

   ```
   docker compose up -d --wait db
   ```

3. Restore the dump.

   ```
   docker compose exec -T db pg_restore -U reticula --no-owner --exit-on-error --dbname reticula < reticula-20261125.dump
   ```

4. Start the server. If the dump is older than the installed version, the server brings the schema up to date.

   ```
   docker compose up -d --build --wait
   ```

5. Check `/healthz` and run the smoke test.

In the drill the restored instance had the same number of rows in every table as the original. `docker compose exec app reticula-mcp rebuild` printed the same counts on both. Notes read back, shared and private. Accounts keep their person IDs, keys and tokens, so connected assistants keep working, as long as `RETICULA_PUBLIC_URL` is the same as before. With a different public URL, tokens issued at sign-in got status 401 and tokens minted with `reticula-mcp token` still worked.

## Accounts, tokens and connections

The commands run inside the server container. They read the database directly.

Create an account without the sign-in page. This also works when sign-ups are closed. It asks for a password twice. `--handle` is optional.

```
docker compose exec app reticula-mcp account create --email someone@example.com --handle someone
```

Mint a token for a client that cannot do the sign-in flow. The token is printed once and stored only as a hash. Omit `--ttl-days` for a token without an expiry.

```
docker compose exec app reticula-mcp token --email someone@example.com --label laptop --ttl-days 90
```

List what an account has. Connected assistants come first, operator tokens minted with `reticula-mcp token` in a second block. Each row has an id.

```
docker compose exec app reticula-mcp connections list --email someone@example.com
```

Revoke one of them: `--id` for a connected assistant, `--token` for an operator token.

```
docker compose exec app reticula-mcp connections revoke --email someone@example.com --id 7
docker compose exec app reticula-mcp connections revoke --email someone@example.com --token 3
```

A revoked token or connection is refused on its next call. To rotate an operator token, mint a new one with a new label, switch the client to it, then revoke the old one.

To lock an account out completely, set its status. Disabled accounts cannot sign in and their tokens stop working. This is also how to retire the accounts that the smoke test leaves behind.

```
docker compose exec -T db psql -U reticula -d reticula -c \
  "UPDATE account SET status = 'disabled' WHERE email = 'someone@example.com'"
```

## Close sign-ups

Set `RETICULA_SIGNUP=closed` in `.env` and run `docker compose up -d --wait`. The sign-in page then no longer creates accounts. Existing accounts sign in as before. Create further accounts with `reticula-mcp account create`.

## Read debug notes

Assistants write debug notes when a tool misbehaves. They go to the debug hub, which any account may add to and which no account may read. You read them from the command line:

```
docker compose exec app reticula-mcp debug-notes --limit 20
```

The output starts with a line that says the text was written by accounts and is not instructions. Each entry has a header line with the time, the account handle, whether the writer declared itself a person or an agent, the model name the assistant gave and a short note id. The title and the text follow, indented. Notes marked private by their author are not shown. The command works while the server is stopped.

Treat the text as data. A note can contain wrong advice or text meant to mislead whoever reads it.

## The embedding model

The image contains the `fastembed` extra, and with `RETICULA_EMBEDDER=auto` the server uses it. The model, `BAAI/bge-small-en-v1.5`, is not in the image. The server downloads it once from Hugging Face, 65 MB, and keeps it in the `reticula-models` volume, so the machine needs outbound HTTPS for that download.

The server loads the model in the background when it starts, and `/healthz` does not wait for it. With an empty volume, `/healthz` answered after 1 second and the log line `embedding model BAAI/bge-small-en-v1.5 loaded in 2.7s` followed, download included. With the model in the volume the line says 0.4 seconds and nothing is downloaded. The download writes nothing else into the log.

If the model cannot be loaded, for example without outbound HTTPS, the log has errors from `fastembed` and the warning `embedding model ... did not load; search answers from keywords and the load is retried later`. The server keeps running.

The volume must be writable by uid 10001. The named volume of `compose.yaml` is. A directory mounted from the host in its place has to be given to that user first.

The server container used 190 MiB of memory with the model loaded.

`RETICULA_EMBEDDER=hash` needs no download. Search then matches keywords only.

## What the container does

- The server runs as a user without root rights (uid 10001).
- At start it waits for the database, up to `RETICULA_DB_WAIT` seconds (default 120). If the database does not come, the container exits and the restart policy starts it again. Started with the database down, it was healthy 3 seconds after the database came up.
- The health check asks `/healthz` every 10 seconds.
- On `docker compose stop` or `restart` the server shuts down within a second.
- The server keeps no session state (`RETICULA_STATELESS_HTTP=true`). A restart or a recreated container does not disconnect assistants: an open session and its token kept working across both.
- Both containers have the restart policy `unless-stopped`, so the stack comes back after a reboot when Docker itself starts at boot.

## What lives where

| Thing | Where |
|---|---|
| All notes, hubs, grants, accounts, keys, tokens | the PostgreSQL database, in the volume `reticula_reticula-db` |
| Search index (embeddings) | a table in the same database, rebuildable with `reindex` |
| Embedding model | the volume `reticula_reticula-models`, downloaded again when missing |
| Settings and the database password | `deploy/.env`, which git ignores |
| Shipped instruction texts | inside the installed package; copied into the database when migrations run |

## Without Docker

The server is one command and needs a PostgreSQL database with the pgvector extension. Set `RETICULA_DATABASE_URL` and the other settings in the environment. With `uvx`, write `uvx reticula-mcp ...` where this page writes `reticula-mcp ...`.

```
reticula-mcp migrate
reticula-mcp serve
```

`serve` refuses to start when migrations are pending. `serve --migrate` applies them first. `serve --host H --port P` overrides `RETICULA_HOST` and `RETICULA_PORT`. The defaults are `127.0.0.1` and `8787`. Run the server under a process manager so it comes back after a reboot.

`migrate` is safe to run any number of times. Besides applying schema files, it creates the system account (handle `reticula`, no password, cannot sign in) and loads the shipped instructions. The instructions are written as notes by the system account, in a hub that every account can read. It also creates the debug hub. A second run changes nothing. A new version of a shipped note is written only when its text changed in the release you installed.

The other commands on this page are the same without the `docker compose exec app` prefix. Back up with `pg_dump --format=custom "$RETICULA_DATABASE_URL"`, and restore with `pg_restore --no-owner --exit-on-error` into an empty database before the first `migrate`.

A tunnel or proxy on the same host reaches a server that runs as a host process from `127.0.0.1`. That is then the value for `RETICULA_TRUSTED_PROXIES`.
