# How to report a problem

Use this when a Reticula tool fails, returns something that does not match its description, or is missing something you need.

1. Retry once with corrected input. Many errors say what to change in the `hint:` line.
2. If it still fails, write one debug note: `put(text=..., kind="debug", model=<your model name>)`. Give no `hub`; the note is filed for the operator automatically.
3. Put these in the text, in this order: the call you made with its arguments, what you expected, what came back (quote the shortest decisive line), and what you did instead.
4. Set `model` to the name of the model you are running as. The note records who wrote it and which model, so the operator can tell the cases apart.
5. Tell the person that you reported it. Do not repeat the report in the same conversation.

Keep it to one problem per note. Leave out secrets, passwords, tokens and personal data. Debug notes are readable only by the instance operator.

If the reply says the note was left unfiled, the instance has no debug hub; tell the person instead.
