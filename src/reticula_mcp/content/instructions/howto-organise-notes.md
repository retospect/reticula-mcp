# How to organise notes into hubs

Use this when notes pile up, or when the person asks where something should go.

- A hub is a note of kind `hub`. Create one with `put(kind="hub", title=<name>, text=<what belongs here>, preset="group")`. Presets: `group` (members read and add), `moderated` (members read and add, only the owner invites), `personal` (only you add, others read).
- Make one hub per project or topic the person returns to. Do not make a hub for fewer than about five notes. Leave those unfiled.
- File a note under a hub when you write it: `put(text=..., hub=<hub>)`. File an older note with `link(ref=<note>, to=<hub>)`.
- Move a note: link it to the new hub, then `link(ref=<note>, to=<old hub>, remove=True)`.
- A hub can sit inside another hub: `link(ref=<child hub>, to=<parent hub>)`. Access to the parent covers the child. A hub cannot end up inside itself.
- Use `tags` for properties that cut across hubs, such as `todo` or `decision`. Use hubs for subjects.
- When several notes say one thing, write one broader note with `supersedes=[<ref>, ...]` and file it under the same hub. Do not delete the old ones.
- Read a hub with `get(ref=<hub>)`. It shows the newest members first.

When you reorganise, tell the person what you moved. Do not move notes out of a hub that someone else owns.
