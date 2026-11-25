# Changelog

## Unreleased

- The server's instructions and the reply of `start` begin with what Reticula is for: the person's own long-term memory, which the next assistant they use reads too, on any model or service. Sharing a hub is named as a separate use. Before, both began with a "shared memory graph". In the trial, assistants filed facts about a project into the project's hub more often (9 of 12 conversations against 4), and nothing private went into a shared hub (`docs/trials.md`, round fourteen).
- The description of `delete` says to delete only the notes that say what the person named, and to keep a note that came up in the same search and says something else. In the trial an assistant deleted such a note with the one asked for in 1 of 72 conversations, against 8 of 72 before (`docs/trials.md`, round thirteen).
- `get` and `search` are faster in a big account. A call now works out once which hubs the account may read. Before, each statement that showed a hub or walked through one worked it out again. At 30,000 notes `get` of a note takes 50 ms instead of 104, `get` of a note that stands three hubs deep in two places 53 ms instead of 224, and `get` of a hub 224 ms instead of 329. A search inside a hub (`hub=`) takes a quarter less time; a search without `hub=` gains little. What a call shows is unchanged.
- `get` of a note names the hub it is filed in by its id at the end of the path: `in: "Lab" / "Lab logistics" id 470547e6`. For a note by somebody else in a hub you may read, it says how to correct it there: `next: put(text=<the correction>, supersedes='2ad253ee', hub='470547e6') replaces it in the same hub`. Before, an assistant asked to correct a note in another person's hub guessed the hub's id in 8 of 24 runs and had calls refused in 9; with these lines it did neither in 24. `docs/trials.md`, round thirteen, has the runs.
- Migrations: `0204_readable_hubs` adds the SQL function `readable_hubs` and redefines `readable_nodes` on top of it. No table, no index and no row is changed, and who may read which note is the same before and after.
- Migrations: `0205_current_among` adds the SQL function `current_among`. It answers which of some given notes are current (readable, and not replaced by a newer note) without working it out for every note. At 30,000 notes that takes 2 ms instead of 67 for 20 notes, and 10 ms instead of 120 to 350 for 400. `get` and the search use it where they ask about a list of notes. No table, no index and no row is changed.
- `search` has a `since` parameter: `7d`, `36h`, `2w` or a date such as `2026-11-20`. `search(query="", since="7d")` lists what was written, edited, filed or shared since then, by you and by other people, newest first, and each line says which of these happened. With words in the query it keeps the results that have changed since then. What was in a hub before it was shared with you is not listed as new; the share is.
- `start` says what other people did in the last seven days, after the hub lists and only when there is something: `New from other people in the last 7 days: 3 in "Kestrel", 1 in @bea's hub id 4f9c21aa. List them with search(query="", since="7d").` A hub shared with you in that time gets a line of its own: `Newly shared with you in the last 7 days: hub id 4f9c21aa by @bea.` Your own hub is named by its title. Another person's hub is named by its owner and the id on its line in the hub list, because its title is that person's text and stays in the list that says so. Nothing is stored about what you have seen.
- Migrations: `0350_recent_events` adds three indexes, on the time a note was last changed, on the time a filing was signed and on the log row of each filing. No table, column, function or row is changed. On 30,000 notes they are built in under 50 ms together.

## 0.2.0 (2026-11-25)

- Request log: a refused sign-in says why in a fixed word, for example `reason="signin: bad_credentials"`, `signin: throttled`, `signin: signup_rate` or `signin: link_gone`. A request refused for its `Host` or `Origin` header has `reason="Invalid Host header"` or `reason="Invalid Origin header"` on its line. The other lines of the log, such as a library's warning, now begin with the time in UTC.
- `reticula-mcp connections list` has a `client` column. It holds what the request log prints as `client=` for that connection.
- Search shows notes that have only some of the words. When fewer than three notes are found, notes with some of the words follow under the line `also, with some of the words:`, and each says which it has (`has: kiln, glaze`). A note is added this way only when one of its words is in at most 50 of the notes searched, so a name alone does not bring every note with that name. When no note has all the words, the first line says so: `no note has all of these words; 2 have some of them`. A note found only because it has half of the words or more was a result in 0.1.1; it now stands under that line.
- When more notes have some of the words than are shown, a last line says how many and how to list them: `2 more have some of the words; a search for one of the words alone lists them`. It counts your own notes, the notes of people you gave access to a hub, and the notes in hubs you have filed into.
- Any other note under that line is by somebody else and stands in a hub you have not filed into, or in one that anyone may file into. It is shown without its text, a note without a title as `(no title)`, and at most three of them are shown, after the others. 0.1.1 listed those with half of the words or more as results with their text, however many there were. Notes found in full, by all the words or by meaning, are listed as before.
- The first line of a search says `1 result` for one result.
- The server's instructions end with one more sentence: ids and addresses are for the assistant's tool calls, and it names notes and hubs to the person by their titles. Before, an assistant that saved or listed notes showed the person an id or an address unasked in most conversations. Now it shows an id only when the person asks or when two notes have the same title. The replies of the tools are unchanged. `docs/trials.md`, round ten, has the runs.
- Migrations: 0.2.0 adds none. A switch from 0.1.1 changes nothing in the database, and 0.1.1 starts again on it.

## 0.1.1 (2026-11-25)

- Access to a hub covers the hubs filed under it. That is so in 0.1.0 too; its entry below does not say it.
- A hub filed under another hub names the people who read it through the hub above. The owner's line in `start` and `profile` reads `shared with @ben, @cleo through a hub above it`. The access list of `share` has `@ben add (through a hub above this one)`, and the `access:` line of `get` has `@ben add (through a hub above)`. A person is named only to someone who may see the access list of that hub above: its owner, or a member who can add to it. Everyone else gets the wording of 0.1.0.
- A member who can add to a hub is told who the people are: `only you and the owner have access`, or `you, the owner and 2 more have access`. 0.1.0 printed `2 people have access` and `4 people have access` for the same hubs.
- `start` and `profile` showed `read` on a hub to two people who can add to it: the owner of a hub above it, and a person with read on the hub and add on a hub above. Both now see `add (from a parent hub)`. The access list says the same of such a reader: `@cleo read (granted by @ben); add through a hub above this one`. Nobody's access changed, only what is printed.
- Search finds a note by a short title inside a long query: `greenhouse gateway SD card replaced` now finds the note titled "Gateway". A title of filler words or numbers only does not match this way.
- Search stays fast in a big account and for a long query. It no longer compares the query with every title: at 30,000 notes the title part of a search takes 48 ms instead of 130, and 11 ms instead of 1.8 s for a 1,000-character query. A search on a long-lived connection no longer slows down after the fifth search (22 to 127 ms measured). A query of more than 16 words that say something is taken for a pasted text: no title is looked for inside it, and a note is still found by its words.
- With Docker, `RETICULA_EMBED_MODEL` in `.env` sets the embedding model.
- Docs: the README quick start is the Docker Compose path; the operating guide starts with cloning the repository at the release tag and has the Tailscale Funnel steps in order.
- `scripts/oauth-client-check` connects with the OAuth client of the MCP Python SDK (sign-in, `start`, token refresh, replay of a used refresh token); Check runs it.
- `start` and `profile` count the notes of a hub as `get` lists them: a deleted note and a note replaced by a newer one are no longer counted, and a hub filed under the hub is named as a hub (`3 notes, 1 hub`).
- The server refuses to start on a database that a newer version has used (a migration in the ledger that this version has no file for), and so does every other command of `reticula-mcp`. `RETICULA_ALLOW_NEWER_SCHEMA=1` starts it anyway. 0.1.0 does not check this.
- The server log has one line per request with what an operator needs to see why an assistant does not connect: the caller's address behind the proxy (as `RETICULA_TRUSTED_PROXIES` allows), the calling program, the MCP protocol version it asked for, the connection of a signed-in request, and the reason for a refusal. It replaces uvicorn's access line, which named the proxy as the caller and gave no reason. `GET /healthz` is no longer logged.
- Migrations: 0.1.1 adds two. `0203_shared_rules` defines three SQL functions and changes no table. `0301_title_words` adds one index on the note table, over the words of each title; no column is added and no row is changed.

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
