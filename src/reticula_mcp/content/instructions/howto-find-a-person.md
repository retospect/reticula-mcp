# How to find a person

Use this when the person wants to share a hub with someone and does not know the exact handle.

1. Run `search(query=<part of the handle>, kind="person")`. An empty query lists everyone who is listed.
2. Each line shows a handle, whether the account is verified, and the day it joined. E-mail addresses are never shown.
3. Handles are chosen by their owners, so a handle proves nothing about who is behind it. Ask the person which account they mean before you share anything.
4. Then run `share(hub=<hub>, person="@name", mode="add")`. Use `mode="read"` when the other person should only read.

Someone who is not listed cannot be found by search. They can still be shared with by their exact handle. The person has to ask them for it.

A new account is not listed. To be listed, the person runs `edit(ref="settings", text="listed: yes")`; to leave the list, `edit(ref="settings", text="listed: no")`. Change this only when the person asks for it.
