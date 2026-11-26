# How to share a hub

Use this when the person wants someone else to read or add to a group of notes.

1. Check that the notes belong in a hub of their own. Use `search(query=..., kind="hub")` to find it, or create it with `put(kind="hub", title=<name>, text=<what the hub is for>, preset="group")`.
2. Ask the person for the other account's handle (`@name`) or person ID. Handles are unique on this instance. Accounts on other instances cannot be added yet. If the person does not know the handle, the instruction "How to find a person" says how to look it up.
3. Run `share(hub=<hub>, person="@name", mode="add")`. Use `mode="read"` when the other person should only read.
4. Tell the person what the other account can now see: every note filed in that hub, and in the hubs under it. Notes written with `private=True` stay hidden.

To see who has access, run `share(hub=<hub>)` without `person`.
To take access away, run `share(hub=<hub>, person="@name", mode="none")`. Notes already written by that person stay in the hub.
To leave a hub someone else shared with the person, run `share(hub=<hub>, person=<the person's own handle>, mode="none")`, but only when the person asks for it. Their notes filed there stay readable to them. The hub "Reticula instructions" cannot be left.
To put a hub on this server's public page, run `share(hub=<hub>, person="public", mode="read")`, but only when the person asks for it. Anyone can then see, without signing in, the titles and ids of the hub, of the person's own sub-hubs in it and of their own notes in it, but not their text. `share(hub=<hub>, person="public", mode="none")` takes it off.

Who may invite: in a `group` hub any member may invite. In a `moderated` hub only the hub's owner may. If `share` answers with a Forbidden error, say so and do not look for a way around it.

Never share a hub the person did not ask you to share. Never share their private notes by copying them into a shared hub.
