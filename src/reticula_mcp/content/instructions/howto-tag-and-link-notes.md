# How to tag and link notes

Use this when notes belong together in a way a hub does not show, or when the person asks you to tag or connect notes.

Links. `link(ref=<A>, to=<B>, rel=<relation>)` records how note A stands to note B. Read it as a sentence: A explains B, A qualifies B. Use one of these names, and the default when no other fits:

- `related-to`: see also. The default; it claims nothing more.
- `supports`: A is evidence for B.
- `explains`: A is the cause or the mechanism of B. "The filter was blocked" explains "The pump stopped".
- `qualifies`: A is an exception to B or a condition on it. "The warranty does not cover filter damage" qualifies "The pump is under warranty".
- `generalises`: A is the broader statement and B a case of it.
- `derived-from`: A was made from B, such as a summary from its sources. Link it once to each source.
- `cites`: A refers to a paper.
- `disputes`: you hold your note A against B. Its main use: someone corrected your note and you do not accept the correction; then your note shows again beside theirs.

Two more are set by the tools and need no link of your own: `part-of` (a note filed under a hub) and `supersedes` (a correction, written with `put(supersedes=...)`).

- Link a note to at most three others. More belongs in the text of the note.
- Do not invent relation names. If none fits, use `related-to`.
- To take a link away: `link(ref=<A>, to=<B>, rel=<relation>, remove=True)`.

Tags. `tags` hold properties that cut across hubs.

- Lowercase, words joined by hyphens: `soil-moisture`, not `Soil Moisture`.
- `topic:` for a subject that notes in several hubs share: `topic:irrigation`. `project:` for an initiative: `project:greenhouse-2027`.
- A few tags per note, two or three. A tag that only repeats the hub's name adds nothing.
- Before you coin a tag, look at the tags already on the person's notes and reuse one that fits.
