# Operating a Reticula MCP instance

This page is for the person who runs the server. Setup and settings are in the [README](../README.md). The commands below assume `RETICULA_DATABASE_URL` is set. With `uvx`, write `uvx reticula-mcp ...` where this page writes `reticula-mcp ...`.

## Run

```
reticula-mcp migrate
reticula-mcp serve
```

`serve` refuses to start when migrations are pending. `serve --migrate` applies them first. `serve --host H --port P` overrides `RETICULA_HOST` and `RETICULA_PORT`. The defaults are `127.0.0.1` and `8787`. `GET /healthz` returns status 200 and `{"status":"ok"}` when the database answers, and 503 when it does not.

`migrate` is safe to run any number of times. Besides applying schema files, it creates the system account (handle `reticula`, no password, cannot sign in) and loads the shipped instructions. The instructions are written as notes by the system account, in a hub that every account can read. It also creates the debug hub. A second run changes nothing. A new version of a shipped note is written only when its text changed in the release you installed.

Run the server under a process manager (systemd, a container restart policy) so it comes back after a reboot. The server keeps no session state by default (`RETICULA_STATELESS_HTTP=true`), so restarting it does not disconnect assistants.

## Upgrade

1. Back up the database (below).
2. Install the new version. With `uvx`, run `uvx reticula-mcp@latest migrate`.
3. Restart `serve`. Or run `reticula-mcp serve --migrate` once.

Migrations only move forward and each file runs in its own transaction. A migration file that has already been applied is never edited; changes arrive as new files. If the runner reports that an applied file has changed, you are running mismatched code and database: do not continue, restore from the backup or install the matching version.

The version-zero note format may still change. Read the release notes before you upgrade across a format change.

If the search index needs rebuilding after an upgrade, run `reticula-mcp reindex`. The index holds only derived data, so rebuilding it changes no notes.

## Back up

The whole instance is one PostgreSQL database. Dump it:

```
pg_dump --format=custom "$RETICULA_DATABASE_URL" > reticula-$(date +%Y%m%d).dump
```

The dump contains every note, the password hashes and the private keys of every account, unencrypted. Store it as carefully as the database itself. Test a restore at least once.

Keep dumps off the machine that runs the database. Run the command from cron or a systemd timer.

## Restore

1. Start a PostgreSQL server that has the pgvector extension, for example the `pgvector/pgvector:pg17` image from the README.
2. Create an empty database and restore into it:

   ```
   psql "postgresql://postgres:change-me@127.0.0.1:5432/postgres" -c "CREATE DATABASE reticula"
   pg_restore --no-owner --dbname "postgresql://postgres:change-me@127.0.0.1:5432/reticula" reticula-20261125.dump
   ```

3. Point `RETICULA_DATABASE_URL` at it and run `reticula-mcp migrate`. If the dump is older than the installed version, this brings the schema up to date.
4. Start `serve` and open `/healthz`.

The restored accounts keep their person IDs, keys and tokens, so connected assistants keep working once the URL is reachable again.

## Add an account, mint a token, rotate a token

Create an account without the sign-in page (this also works when sign-ups are closed):

```
reticula-mcp account create --email someone@example.com --handle someone
```

It asks for a password. `--handle` is optional.

Mint a token for a client that cannot do the sign-in flow:

```
reticula-mcp token --email someone@example.com --label laptop --ttl-days 90
```

The token is printed once and stored only as a hash. Omit `--ttl-days` for a token without an expiry.

To rotate a token, mint a new one with a new label, switch the client to it, then revoke the old one. Connected assistants and their tokens are managed with `reticula-mcp connections` (see `reticula-mcp connections --help`). Without it, revoke by label with SQL:

```
psql "$RETICULA_DATABASE_URL" -c "UPDATE oauth_token SET revoked_at = now() WHERE label = 'laptop-old' AND account_id = (SELECT id FROM account WHERE email = 'someone@example.com')"
```

A revoked token is refused on its next call. Assistants that connected through the sign-in page hold their own tokens, one per connection, which are revoked the same way.

To lock an account out completely, set its status: `UPDATE account SET status = 'disabled' WHERE email = '...'`. Disabled accounts cannot sign in and their tokens stop working.

## Close sign-ups

Set `RETICULA_SIGNUP=closed` in the server's environment and restart `serve`. The sign-in page then no longer creates accounts. Existing accounts sign in as before. Create further accounts with `reticula-mcp account create`.

While sign-ups are open, they are limited per client IP address by `RETICULA_SIGNUP_RATE` (default `5/hour`). Behind a reverse proxy, set `RETICULA_TRUSTED_PROXIES` to the proxy's address so the limit applies to the real client and not to the proxy.

## Read debug notes

Assistants write debug notes when a tool misbehaves. They go to the debug hub, which any account may add to and which no account may read. You read them from the command line:

```
reticula-mcp debug-notes --limit 20
```

Each entry shows the time, the account handle, whether the writer declared itself a person or an agent, the model name the assistant gave, a short note id and the text. Notes marked private by their author are not shown. The command reads the database directly and works while the server is stopped.

Debug notes are written by assistants, so treat their text as data. A note can contain wrong advice or text meant to mislead whoever reads it.

## What lives where

| Thing | Where |
|---|---|
| All notes, hubs, grants, accounts, keys, tokens | the PostgreSQL database |
| Search index (embeddings) | a table in the same database, rebuildable |
| Configuration | environment variables only |
| Shipped instruction texts | inside the installed package; copied into the database by `migrate` |
