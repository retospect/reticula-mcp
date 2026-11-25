# Changelog

## Unreleased

- Access to a hub covers the hubs filed under it. That is so in 0.1.0 too; its entry below does not say it.
- Search finds a note by a short title inside a long query: `greenhouse gateway SD card replaced` now finds the note titled "Gateway". A title of filler words or numbers only does not match this way.
- With Docker, `RETICULA_EMBED_MODEL` in `.env` sets the embedding model.
- Docs: the README quick start is the Docker Compose path; the operating guide starts with cloning the repository at the release tag and has the Tailscale Funnel steps in order.
- `scripts/oauth-client-check` connects with the OAuth client of the MCP Python SDK (sign-in, `start`, token refresh, replay of a used refresh token); Check runs it.
- `start` and `profile` count the notes of a hub as `get` lists them: a deleted note and a note replaced by a newer one are no longer counted, and a hub filed under the hub is named as a hub (`3 notes, 1 hub`).
- The server refuses to start on a database that a newer version has used (a migration in the ledger that this version has no file for), and so does every other command of `reticula-mcp`. `RETICULA_ALLOW_NEWER_SCHEMA=1` starts it anyway. 0.1.0 does not check this.
- The server log has one line per request with what an operator needs to see why an assistant does not connect: the caller's address behind the proxy (as `RETICULA_TRUSTED_PROXIES` allows), the calling program, the MCP protocol version it asked for, the connection of a signed-in request, and the reason for a refusal. It replaces uvicorn's access line, which named the proxy as the caller and gave no reason. `GET /healthz` is no longer logged.
- Migrations: 0.1.1 adds one, `0203_shared_rules`. It defines three SQL functions and changes no table.

## 0.1.0 (2026-11-25)

The first release. It is phase one of Reticula: one server that gives AI assistants a long-term memory they can share.

What phase one is:

- An MCP server over streamable HTTP with nine tools: `start`, `profile`, `search`, `get`, `put`, `edit`, `link`, `delete` and `share`. Replies are compact text. Errors read `[error:Code] message` with a `hint:` line.
- Notes (atoms) with a permanent address, `ret:<person id>/<uuid>`. A note is edited by its author as a new version. Anyone else corrects it by writing a note that supersedes it. Old versions and superseded notes stay readable by address.
- Hubs to file notes under, with three presets (`group`, `moderated`, `personal`). A hub is shared with other accounts on the same instance for reading and adding, or for reading only. A note marked private stays with its author even inside a shared hub.
- Search over everything an account may read, by keywords and by meaning. Each result shows an excerpt of the note, so a short note is read without a second call. A search without words is a listing: `search(query="", kind="hub")` lists every hub the account can read, and `search(query="", hub=<hub>)` lists what is filed under one hub. A DOI or arXiv id as the query finds its paper and the notes about it. The extra `reticula-mcp[fastembed]` adds a local embedding model. Without it, keyword matching still works.
- Accounts with an e-mail address and a password, a sign-in page for assistant connectors, and bearer tokens minted on the command line. Connected assistants and tokens can be listed and revoked one by one.
- An Ed25519 signature on every note from the moment it is written. The note format is version zero and is specified in `docs/format.md`.
- Instructions for assistants that ship with the server: a starting instruction returned by `start`, and how-to notes found with `search(kind="instruction")`. Text written by anyone else is quoted as data and is never an instruction.
- Debug notes. An assistant can report a tool that misbehaved, and only the operator reads the report (`reticula-mcp debug-notes`).
- Paper stubs by DOI or arXiv id, and notes anchored to a quoted passage of a paper. A note shows the paper it is about, and a paper lists its notes.
- PostgreSQL 17 with pgvector as the only store, migrations with `reticula-mcp migrate`, and a compose file that runs database and server together.
- Trials with assistants that had never seen Reticula, on Claude Sonnet and Claude Haiku. `docs/trials.md` records what they did and what was changed because of it.

What phase one is not:

- No federation. Two instances do not exchange notes. Addresses and signatures are in place so that it can be added without rewriting notes.
- No encryption. The format has room for it and nothing uses it. Anyone who can read the database can read every note.
- No web interface except the sign-in page.
- No messaging, no review workflow, no paper library and no installer.
- No stable format yet. Version zero of the note format may still change.
- No promise about assistant products other than the ones tried. Check the connector with your own client.
