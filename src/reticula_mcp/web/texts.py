"""The fixed texts of the public pages. Plain HTML written here; the only
value put in from outside is the server's own address on /connect, escaped."""

from __future__ import annotations

from urllib.parse import urlsplit

from reticula_mcp.web.html import HOME, REPO, e

#: The line of the onboarding how-to (content/instructions/
#: howto-use-reticula-as-memory.md), as the person pastes it.
STANDING_LINE = (
    "Use the Reticula connector as long-term memory: call its start tool at the"
    " beginning of every conversation, search it before answering from memory,"
    " and save there what should outlast the conversation."
)

FRONT = f"""
<h1>Reticula</h1>
<p class="lead">A long-term memory that belongs to you, readable by any
assistant that speaks the Model Context Protocol (MCP).</p>
<p>Your notes live on a server you run or choose, not inside one assistant.
Claude Code, claude.ai, the Claude apps and other MCP clients read and write
the same notes, so a new model or a new service starts with what the old one
knew. The assistant saves decisions, facts and results as you work, and says
in one line what it saved.</p>
<p>Notes are small and signed. Each has a permanent address that names the
person who wrote it, not the server. Hubs group notes, and a hub can be shared
with other people on the same server, to read or to add to.</p>
<ul>
<li><a href="/connect">Connect an assistant</a>: sign up and add the connector.</li>
<li><a href="/slides">Slides</a>: four pages on what it is and how it works.</li>
<li><a href="/hubs">Public hubs</a> on this server.</li>
<li><a href="/vision">Vision</a>: what it is for, and what is designed but not built.</li>
<li><a href="/privacy">Privacy</a>: what the server stores and who can read it.</li>
<li><a href="{REPO}">Source on GitHub</a>, licensed AGPL-3.0-or-later; you
can run your own server.</li>
<li><a href="{HOME}">reticula.to</a></li>
</ul>
"""


def connect(base_url: str) -> str:
    mcp = e(f"{base_url}/mcp")
    host = e(urlsplit(base_url).netloc or base_url)
    line = e(STANDING_LINE)
    return f"""
<h1>Connect an assistant</h1>
<p>The connector address of this server:</p>
<pre><code>{mcp}</code></pre>
<p>You create your account the first time you connect, with an e-mail address
and a password.</p>

<h2>The Claude app on iPhone</h2>
<p>The same steps work in the Android app and on claude.ai.</p>
<ol>
<li>Tap your user icon (your initials, bottom left).</li>
<li>Scroll down to Connectors and tap it.</li>
<li>Tap + (top right), then Add custom connector.</li>
<li>Name: Reticula. MCP server URL: <code>{mcp}</code>. Tap Continue.</li>
<li>Choose Sign in now and Register automatically, scroll down and tap Add.</li>
<li>Claude asks to use {host} to sign in: tap Continue.</li>
<li>Tap Create account, type your e-mail address and pick a new password.</li>
<li>Optional: let Claude use Reticula without asking each time. By default it
asks for every note it writes or looks up. That is your choice.</li>
</ol>
<p>Then, once: an assistant uses a connector only when a conversation calls for
it. For Claude to use Reticula in every conversation, give it this standing
line. Paste it into your personal preferences in the app's settings, or, if
Claude's memory is on, ask Claude to remember it.</p>
<blockquote>{line}</blockquote>
<p class="small">The first conversation through a new connection points the
assistant to an instruction that offers to do this with you.</p>

<h2>Claude Code</h2>
<pre><code>claude mcp add --transport http reticula {mcp}</code></pre>
<p>Then type <code>/mcp</code> inside Claude Code, choose reticula and sign in
in the browser it opens. Put the standing line above in your CLAUDE.md file.</p>
<p class="small">The command is the one in Claude Code's MCP documentation
(code.claude.com/docs/en/mcp) and in the help of Claude Code 2.1.323, read on
2026-11-26. The sign-in was not run for this page.</p>

<h2>OpenAI Codex CLI</h2>
<pre><code>codex mcp add reticula --url {mcp}
codex mcp login reticula</code></pre>
<p>The first command writes this to <code>~/.codex/config.toml</code>:</p>
<pre><code>[mcp_servers.reticula]
url = "{mcp}"</code></pre>
<p class="small">From OpenAI's Codex MCP documentation, read on 2026-11-26. The
add command was run with codex-cli 0.171.0 and wrote the lines above; the
login was not tested.</p>

<h2>Kimi Code CLI</h2>
<p>Add the server to <code>~/.kimi-code/mcp.json</code>:</p>
<pre><code>{{
  "mcpServers": {{
    "reticula": {{ "url": "{mcp}" }}
  }}
}}</code></pre>
<p>Then, in Kimi Code, run <code>/mcp-config login reticula</code> and sign in
in the browser.</p>
<p class="small">From Moonshot AI's Kimi Code documentation (MCP page), read on
2026-11-26. Not tested.</p>

<h2>Any other MCP client</h2>
<p>Add a remote MCP server with the address above, over streamable HTTP. The
client signs in with OAuth, and the server registers it on the spot: there is
no client ID or secret to enter. Most major assistants have a setting like
this.</p>

<h2>How to use it</h2>
<p>One memory across your apps and models. Talk a spec through with the Claude
app while you walk the dog; later, at the terminal, Claude Code finds it in the
same memory and does the work. Each assistant reads what the others saved, and
nothing is lost when you change the model or the app.</p>
"""


VISION = """
<h1>Vision</h1>
<p>Reticula is a memory that belongs to the person, not to the assistant. It
should outlast any one model, service or company, and any assistant the person
uses should be able to read and add to it.</p>

<h2>What it is today</h2>
<ul>
<li>A server you run yourself or use on someone else's machine. It is open
source; one PostgreSQL database holds everything, and one dump of it is the
whole backup.</li>
<li>Small notes, each signed with the author's key (Ed25519), each with an
address that names the person and stays the same for good. A correction is a
new note that supersedes the old one; nobody edits another person's note.</li>
<li>Hubs group notes and can sit inside one another. A hub can be shared,
person by person, to read or to read and add.</li>
<li>Search by words and by meaning, with the meaning computed on the server
itself; no note is sent to another service for it.</li>
<li>Measured on a test account of 30,000 notes in 20 hubs: listing what changed
in the last seven days takes about 20 ms on the server; reading a hub of 1,000
notes in lettered groups takes about 130 ms.</li>
</ul>

<h2>Designed, not built</h2>
<p>The following is written down in the specification and is not in the
software yet.</p>
<ul>
<li><b>Federation.</b> Each person's server publishes their signed notes as a
feed, and peers pull what is new from each other. No server holds a master
copy: the signature, not the host, proves who wrote a note. Any member's server
can be the always-on peer of a group. The protocol is Reticula's own signed
feeds; ActivityPub may be used only to announce new public notes, not to carry
the data.</li>
<li><b>Encrypted hubs.</b> Each hub has a key, wrapped for each member with a
post-quantum hybrid scheme. Removing a member means a new key for those who
stay; later notes are then unreadable to the one who left.</li>
<li><b>Deletion across servers.</b> A deletion is a signed tombstone that
servers honour in good faith: they drop the content and keep the bare address.
What another person already copied cannot be taken back.</li>
<li><b>Trust between people.</b> Four proposals, none confirmed:
  <ul>
  <li>A vouch: a signed note that a key belongs to a person, saying how it was
  checked (in person, at an event, by e-mail, or by a proof on an ORCID record
  or a university domain).</li>
  <li>Trust that weights only the reader's own ranking: a note supported by
  people the reader vouched for may rank higher for that reader. No score is
  published, and there is no public score of a person.</li>
  <li>Invites as the trust graph: a server accepts the feeds of people its own
  people invited, and an operator can cut a branch that sends spam.</li>
  <li>Recovery of a lost key: a successor key that a set of named people
  co-sign, for example three of five.</li>
  </ul>
</li>
</ul>
"""


PRIVACY = """
<h1>Privacy</h1>
<p>This describes the Reticula software. The operator of a server decides who
may sign up and where the server runs; ask them about their own backups and
logs.</p>

<h2>What the server stores</h2>
<ul>
<li>Your account: e-mail address, a password hash (Argon2), your handle, your
person ID, and your keys. The server holds your private keys itself, unencrypted,
so that it can sign your notes; whoever runs the server can therefore act for
you.</li>
<li>Your notes, hubs, links and shares, each as a signed record, with every
earlier version of a note.</li>
<li>For search: a vector of each note, computed on the server.</li>
<li>For sign-in: the connections of your assistants, with their tokens stored
hashed, so that each can be revoked.</li>
<li>A log line per request for the operator: method, path without its query,
status, time, the caller's address, the program's name and, for a signed-in
connection, the start of its client id. Never a note, a token or what a tool
was asked or answered.</li>
</ul>

<h2>Who can read what</h2>
<ul>
<li>A note is read by its author, and by the people the hub it is filed in was
shared with, and the hubs above it. A note marked private is read by its
author alone, wherever it is filed.</li>
<li>Filing someone else's note in your hub only organises your own view: it
shows it to nobody new.</li>
<li>The operator of the server can read the database. Notes are not encrypted
at rest by Reticula itself in this version.</li>
<li>No note is sent to another company by the server. The assistant you
connect does read what it asks for: what your assistant's provider does with
it is under that provider's terms.</li>
</ul>

<h2>What these public pages show</h2>
<p>Nothing about any person. <a href="/hubs">Public hubs</a> lists only the
hubs whose owners made them public, with the titles and short ids of the hub,
the owner's own sub-hubs and the owner's own notes in them. It shows no text,
no handle, no date and nothing private. Notes by other people in a public hub
are left out. The pages set no cookie and run no script.</p>

<h2>Deleting</h2>
<p>Deleting a note erases its content in every stored version; its address
remains and answers as deleted, so that links to it do not break. A share is
taken back with mode none and takes effect from the next call. Copies that
another person already made, for example in their own notes or their assistant's
answers, are not reached by a deletion.</p>

<h2>Backups</h2>
<p>A backup is a dump of the database: it holds everything above, including
notes deleted after the backup was made. How long backups are kept is the
operator's decision.</p>
"""
