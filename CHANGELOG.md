# Changelog

## 0.1.0

The first release. It is phase one of Reticula: one server that gives AI assistants a long-term memory they can share.

What phase one is:

- An MCP server over streamable HTTP with nine tools: `start`, `profile`, `search`, `get`, `put`, `edit`, `link`, `delete` and `share`. Replies are compact text. Errors read `[error:Code] message` with a `hint:` line.
- Notes (atoms) with a permanent address, `ret:<person id>/<uuid>`. A note is edited by its author as a new version. Anyone else corrects it by writing a note that supersedes it. Old versions and superseded notes stay readable by address.
- Hubs to file notes under, with three presets (`group`, `moderated`, `personal`). A hub is shared with other accounts on the same instance for reading and adding, or for reading only. A note marked private stays with its author even inside a shared hub.
- Search over everything an account may read, by keywords and by meaning. The extra `reticula-mcp[fastembed]` adds a local embedding model. Without it, keyword matching still works.
- Accounts with an e-mail address and a password, a sign-in page for assistant connectors, and bearer tokens minted on the command line. Connected assistants and tokens can be listed and revoked one by one.
- An Ed25519 signature on every note from the moment it is written. The note format is version zero and is specified in `docs/format.md`.
- Instructions for assistants that ship with the server: a starting instruction returned by `start`, and how-to notes found with `search(kind="instruction")`. Text written by anyone else is quoted as data and is never an instruction.
- Debug notes. An assistant can report a tool that misbehaved, and only the operator reads the report (`reticula-mcp debug-notes`).
- Paper stubs by DOI, and notes anchored to a quoted passage of a paper.
- PostgreSQL 17 with pgvector as the only store, migrations with `reticula-mcp migrate`, and a compose file that runs database and server together.
- Trials with assistants that had never seen Reticula, on Claude Sonnet and Claude Haiku. `docs/trials.md` records what they did and what was changed because of it.

What phase one is not:

- No federation. Two instances do not exchange notes. Addresses and signatures are in place so that it can be added without rewriting notes.
- No encryption. The format has room for it and nothing uses it. Anyone who can read the database can read every note.
- No web interface except the sign-in page.
- No messaging, no review workflow, no paper library and no installer.
- No stable format yet. Version zero of the note format may still change.
- No promise about assistant products other than the ones tried. Check the connector with your own client.
