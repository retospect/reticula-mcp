"""The public web pages: what Reticula is, how to connect, the public hubs.

Plain HTML written on the server: no JavaScript, no cookies, no forms, no
data about any person. The only data shown is /hubs, from ``public_nodes()``
(migration 0354): titles and ids of hubs their owners made public and of the
owner's own notes in them. Every value is escaped (``html.escape``), and every
page carries a strict Content-Security-Policy.
"""
