# Operating a Reticula MCP instance

This page is for the person who runs the server. Settings are listed in the [README](../README.md).

The commands are written for the Docker Compose files in [`deploy/`](../deploy) and are run from that directory. They were run as written here against a throwaway copy of the stack, with Docker Compose 5.8 on macOS, where Docker runs inside a Linux virtual machine. On Linux, the project's Check run on GitHub builds the image from the checkout, starts the stack of `compose.dev.yaml` and runs the smoke test against it on every change. On 2026-11-25 that was Ubuntu 26.04 with Docker Compose 5.1.3. No older version of Compose was tried. To run the server without Docker, see [Without Docker](#without-docker) at the end.

## Start

1. Get the files. They are in the repository on GitHub; the package on PyPI does not contain them. Clone the repository at the tag of the release you want to run, and go to its `deploy` directory.

   ```
   git clone --branch v<release> --depth 1 https://github.com/retospect/reticula-mcp.git
   cd reticula-mcp/deploy
   ```

   For release 0.1.1 the option is `--branch v0.1.1`. The releases are listed on [PyPI](https://pypi.org/project/reticula-mcp/#history). `git` prints a warning that the tag is not a commit and a note about a detached HEAD. Both are expected.

   The files under `deploy/` at a tag belong to that release, and so does this guide. Read it at the same tag: the main branch can describe things a release does not have yet.

2. Create the settings file.

   ```
   cp .env.example .env
   ```

   Set two values in `.env`:

   - `RETICULA_DB_PASSWORD`: generate one with `openssl rand -hex 24`.
   - `RETICULA_VERSION`: the release you cloned, without the `v`. For the tag `v0.1.1` it is `0.1.1`.

   Leave `RETICULA_PUBLIC_URL` and `RETICULA_TRUSTED_PROXIES` empty for now.

   If port 8787 is taken on your machine, set `RETICULA_PORT` to a free one. It is the port published on `127.0.0.1`. Write it in place of 8787 in every command of this guide. Inside the container the server stays on 8787, and its log names that port.

3. Start the database and the server.

   ```
   docker compose up -d --build --wait
   ```

   To run a checkout instead of a release, add the override file. Use the same two `-f` options on every later `docker compose` command.

   ```
   docker compose -f compose.yaml -f compose.source.yaml up -d --build --wait
   ```

   The build of release 0.1.1 took 22 seconds, the first build of a checkout 30 seconds. With the image built, the command returned after 11 to 13 seconds, with both containers healthy. The server container waits for the database, applies pending migrations and then serves.

   `docker compose ps` shows the state of both containers. If the command does not return, or ends with an error, read `docker compose logs app` and `docker compose logs db`. With `RETICULA_DB_PASSWORD` left empty it stops at once with `required variable RETICULA_DB_PASSWORD is missing a value`.

   The compose file fixes the project name to `reticula`. A second copy on the same machine needs a name of its own, or its commands act on the containers of the first copy. Give it one with `docker compose -p <name>` on every command, or by setting `COMPOSE_PROJECT_NAME`, and give it its own `RETICULA_PORT` and `RETICULA_SUBNET` in its `.env`.

4. Check health.

   ```
   curl -s -w '\n%{http_code}\n' http://127.0.0.1:8787/healthz
   ```

   The answer is `{"status":"ok","version":"0.1.1"}`, with the installed version, and `200` on the next line. When the database does not answer, it is `{"status":"unavailable","version":"0.1.1"}` and `503`. `docker compose ps` shows the same result as `healthy` or `unhealthy`.

5. Run the smoke test. It needs [uv](https://docs.astral.sh/uv/).

   ```
   ../scripts/smoke http://127.0.0.1:8787
   ```

   It walks the path an assistant takes: discovery, client registration, sign-up, token exchange, then `start`, `put`, `search`, `get` and `delete` over MCP, and it revokes its tokens at the end. It prints one line per step, 14 in all. Each must start with `ok`.

   Each run leaves one account named `smoke-<id>@example.com`, one registered client and one erased note. The last lines of the output list them. Each run also counts as one sign-up from your address, so with the default limit the sixth run within an hour fails at `sign-up` with status 429. To sign in to an account from an earlier run instead, pass `--email` and put its password in `SMOKE_PASSWORD`.

   A second script, `../scripts/oauth-client-check`, takes the same arguments and lets the OAuth client of the MCP Python SDK sign in, call `start` and renew its token; it prints 12 lines that must start with `ok`, and it leaves an account named `sdk-<id>@example.com` and counts as a sign-up too.

6. Decide how assistants reach the server before you connect the first one. An assistant that runs on this machine can use `http://127.0.0.1:8787/mcp` as it is. For every other assistant, do the next section first: a later change of the public address disconnects every assistant that signed in.

7. Connect an assistant and create the first account. The steps are in the README, under [Connecting an assistant](../README.md#connecting-an-assistant). To create an account from the command line instead, see [Accounts, tokens and connections](#accounts-tokens-and-connections).

## Put a public name in front

The server is published on `127.0.0.1` only and speaks plain HTTP. A tunnel or a reverse proxy on the same machine terminates TLS and forwards to `http://127.0.0.1:8787`.

Do this before the first assistant connects. Once the public URL is set, an assistant can no longer sign in through `http://127.0.0.1:8787/mcp`, and a later change of the URL disconnects every assistant that signed in.

1. Point the tunnel or proxy at `http://127.0.0.1:8787`. For Tailscale Funnel the commands are under [Tailscale Funnel](#tailscale-funnel) below, as one list that includes the steps here. Any other tunnel or reverse proxy that terminates TLS and forwards plain HTTP fits as well. This guide has no commands for those.

2. Set two values in `.env`. The first is the address people use, with the port if it is not 443 and without a trailing slash. The second is explained in the next section.

   ```
   RETICULA_PUBLIC_URL=https://notes.example.com
   RETICULA_TRUSTED_PROXIES=172.31.88.1
   ```

3. Apply them. This recreates the server container only.

   ```
   docker compose up -d --wait
   ```

4. Run the smoke test against the public name.

   ```
   ../scripts/smoke https://notes.example.com
   ```

   All 14 lines must start with `ok`, and the `authorize` line must end with `cookie Secure`.

5. Check that client addresses are counted correctly, with the two commands under [Check the setting](#check-the-setting).

What changes once `RETICULA_PUBLIC_URL` is set:

- Every address the server hands out starts with the public URL: the OAuth metadata (OAuth is the sign-in protocol an assistant's connector uses), the sign-in redirect and the `WWW-Authenticate` challenge. This holds for requests that arrive on the loopback port too. The smoke test against `http://127.0.0.1:8787` therefore fails at `discovery` from now on, and so does the sign-in of an assistant that was given the loopback address. A token minted with `reticula-mcp token` still works on the loopback port. `/healthz` on the loopback port keeps working.
- The sign-in cookie is marked `Secure` whenever the public URL starts with `https://`.
- Tokens issued at sign-in are bound to the public URL. After a change of the URL, every connected assistant gets status 401 and has to connect again. Tokens minted with `reticula-mcp token` are not bound and keep working.
- `/mcp` answers only requests whose `Host` header is the host and port of the public URL, or a loopback name such as `127.0.0.1:8787`. See the table below.

## A tunnel or proxy on the same host

The limits on sign-ups, client registrations and failed sign-ins are counted per client address. Behind a tunnel or proxy the server sees only the proxy's address, unless it is told which peer to believe. `RETICULA_TRUSTED_PROXIES` names the peers whose `X-Forwarded-For` header is believed.

| Setup | `RETICULA_TRUSTED_PROXIES` |
|---|---|
| The tunnel or proxy is a process on the host, forwards to `127.0.0.1:8787` and sets `X-Forwarded-For` | `172.31.88.1` |
| The proxy is a container in the compose network and sets `X-Forwarded-For` | the address of that container (give it a fixed one) |
| The tunnel forwards plain TCP, or sets no `X-Forwarded-For` | empty |

`172.31.88.1` is the gateway of the compose network. A process on the host that connects to the published port reaches the server from that address, not from `127.0.0.1`. That was measured on macOS and on Linux: in the Check run of 2026-11-25 (Ubuntu 26.04, Docker Compose 5.1.3) a request from the runner to the published port of the dev stack arrived from `172.31.87.1`, the gateway of that stack's network. `compose.yaml` fixes the subnet of its network to `172.31.88.0/24` so that this address stays the same.

Measured, with the gateway address trusted and requests sent from the host to the published port:

| `X-Forwarded-For` sent | Address the request was counted under |
|---|---|
| `203.0.113.7` | `203.0.113.7` |
| `198.51.100.9, 203.0.113.7` | `203.0.113.7` (the last address that is not a trusted proxy) |
| two header lines, `198.51.100.9` then `203.0.113.8` | `203.0.113.8` |
| `2001:db8:1:2:3:4:5:6` | `2001:db8:1:2::/64` (IPv6 clients are counted per /64) |
| none, or a value that is not an address | the gateway address |

Measured, with a Caddy container as the proxy and two clients behind it, and the sign-up limit set to 2 per hour:

- Proxy address trusted: each client was counted under its own address. The third sign-up of the first client got status 429. The second client could still sign up. A client that sent its own `X-Forwarded-For` through the proxy was still counted under its real address.
- Nothing trusted: both clients were counted under the proxy's address. After two sign-ups by the first client, the second client got status 429.

What follows from that:

- With `RETICULA_TRUSTED_PROXIES` empty, all clients behind the proxy share one bucket. A few sign-ups or failed sign-ins by anyone lock everyone out for the window. The server writes a warning to its log at start when the public URL is `https` and the setting is empty.
- Trust a peer only if it writes `X-Forwarded-For` itself, by replacing the header or by appending to it. If you trust a tunnel that passes the client's own header through, or that sets none, every client can choose the address it is counted under and the limits stop working. Leave the setting empty in that case.
- Trusting the gateway address trusts every process on the host that can reach the published port.

The proxy must leave the `Host` header as the client sent it. Measured on `/mcp` with a valid token and `RETICULA_PUBLIC_URL=https://notes.example.com`:

| `Host` header | Status |
|---|---|
| `notes.example.com` | 200 |
| `127.0.0.1:8787` | 200 |
| `notes.example.com:443` | 421 |
| `app:8787` (the name of the upstream) | 421 |
| any other name | 421 |

When the public URL carries a port, the `Host` header must carry it too. A request with an `Origin` header from another site gets status 403. The sign-in page, the OAuth endpoints and `/healthz` do not check `Host`. The addresses they return come from `RETICULA_PUBLIC_URL`, never from the request.

### Check the setting

First check which address the server sees for the host. A request for `/healthz` leaves no line in the log, so ask for another address:

```
curl -s -o /dev/null -A 'address check' http://127.0.0.1:8787/.well-known/oauth-authorization-server
docker compose logs app | grep 'address check' | tail -1
```

The line looks like this:

```
app-1  | 2026-11-25T13:13:50Z GET /.well-known/oauth-authorization-server 200 0ms from=172.31.88.1 agent="address check"
```

The address after `from=`, here `172.31.88.1`, is the one to trust.

Then check the whole path, with `RETICULA_TRUSTED_PROXIES` set to that address. Register a client through the public name, with a made-up `X-Forwarded-For`, and read the address it was counted under. The first command prints `201`.

```
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://notes.example.com/register \
  -H 'Content-Type: application/json' -H 'X-Forwarded-For: 203.0.113.99' \
  -d '{"client_name":"proxy check","redirect_uris":["http://127.0.0.1:53682/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}'
docker compose exec -T db psql -U reticula -d reticula -c \
  "SELECT subject, created_at FROM auth_event WHERE kind = 'register_ip' ORDER BY created_at DESC LIMIT 3"
```

| Newest `subject` | Meaning |
|---|---|
| `203.0.113.99` | the tunnel passes client headers through: set `RETICULA_TRUSTED_PROXIES` back to empty |
| the gateway address | no header arrives: leave the setting empty, all clients share one bucket |
| any other address | correct: it is the address the tunnel saw the `curl` come from. Through a public tunnel that is the public address of the machine you ran it on |

### Change the subnet

If `172.31.88.0/24` collides with a network you already have, set `RETICULA_SUBNET` in `.env`. The gateway is the first address of the subnet.

A change of the subnet does not apply cleanly to a running stack. Compose recreates the network, but the running containers lose their names on it: the server logs `waiting for the database` and `docker compose up --wait` does not return. This also happens on the first `up` after an update that introduced the fixed subnet. Take the stack down first. The volumes stay.

```
docker compose down
docker compose up -d --build --wait
```

Never add `-v` to `down`: it deletes the database. If the stack is already stuck, the same two commands repair it. It was back and healthy after 11 seconds with its data.

### Tailscale Funnel

Funnel is the part of [Tailscale](https://tailscale.com/) that gives a machine a public HTTPS name and forwards requests from the internet to a port on that machine. The machine must be in a tailnet, which is Tailscale's word for your private network of machines.

Each statement about Tailscale below is one of two kinds. "Measured" was seen on a running instance of this server behind Funnel, with Tailscale 1.104.7 on 2026-11-25. "Tailscale documentation" was read on 2026-11-25 in Tailscale's pages on [Funnel](https://tailscale.com/kb/1223/funnel), the [`tailscale funnel` command](https://tailscale.com/kb/1311/tailscale-funnel) and [HTTPS certificates](https://tailscale.com/kb/1153/enabling-https), and was not tried for this guide.

1. Switch on what Funnel needs (Tailscale documentation):

   - Tailscale 1.38.3 or later on the machine.
   - MagicDNS for the tailnet.
   - HTTPS certificates for the tailnet: in the admin console, on the DNS page, "Enable HTTPS" under "HTTPS Certificates".
   - The `funnel` node attribute for this machine in the tailnet policy file. If it is missing, the command of the next step opens a web page that offers to add it. It can also be added on the "Access controls" page of the admin console.

2. Start the tunnel, on the machine that runs the server.

   ```
   tailscale funnel --bg --https=<port> http://127.0.0.1:8787
   ```

   Measured: this form of the command, with a port other than 443. It may need `sudo`. Tailscale documentation: `<port>` is 443, 8443 or 10000, and Funnel offers no other port. `--bg` keeps the tunnel running in the background, also after a restart of the machine. The command prints the public address after the line `Available on the internet:`. The name is the machine's own, `<machine>.<tailnet>.ts.net`. Funnel cannot serve a domain of your own.

   Use 443 unless the machine already serves something else on it. See the note on ports below.

3. Set the public URL in `.env` to the address the command printed, without a trailing slash, and with the port if it is not 443. Trust the gateway address.

   ```
   RETICULA_PUBLIC_URL=https://<machine>.<tailnet>.ts.net
   RETICULA_TRUSTED_PROXIES=172.31.88.1
   ```

4. Apply the settings. This recreates the server container only.

   ```
   docker compose up -d --wait
   ```

5. Check the result.

   ```
   tailscale funnel status
   ../scripts/smoke https://<machine>.<tailnet>.ts.net
   ```

   The first command lists the tunnel (Tailscale documentation). The smoke test must print 14 lines that start with `ok`. Then run the two commands under [Check the setting](#check-the-setting).

What else to know:

- Ports. A port other than 443 works only for assistants that connect from your own machines, such as Claude Code. Claude's custom connectors (claude.ai, the desktop and mobile apps) connect from Anthropic's servers and accept port 443 only: "Claude can't connect to a URL that specifies another port" (Claude support, "Get started with custom connectors using remote MCP", read on 2026-11-25). For those, run Reticula on a machine whose port 443 is free, or put a reverse proxy with a name of its own in front. On a machine that already uses Funnel on 443, a second service goes on 8443 or 10000, and `RETICULA_PUBLIC_URL` must then carry that port.
- Client addresses. Measured: in its HTTPS mode Funnel writes `X-Forwarded-For` itself and replaces a value sent by the client. A request with a made-up header was counted under the caller's real address. With `tailscaled` on the host this is the first row of the settings table under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host): trust the gateway address. Its TCP modes add no header (read from the Tailscale source, not measured): leave `RETICULA_TRUSTED_PROXIES` empty for those.
- The `Host` header. Measured: Funnel passes the header on with its port, which is what `/mcp` expects when `RETICULA_PUBLIC_URL` carries the port.
- Turning it off. To stop one tunnel, run its command again with `off` at the end. `tailscale funnel reset` clears the whole Funnel configuration of the machine, not one tunnel (both Tailscale documentation).
- `tailscale serve` is the same tunnel for the machines of your tailnet only, without the public internet. Tailscale's pages on Serve do not say whether it writes `X-Forwarded-For`, and it was not measured here. Set `RETICULA_TRUSTED_PROXIES` to the gateway address, run [Check the setting](#check-the-setting), and keep the value only if the table there says it is correct.

## Limits

| Limit | Setting or value |
|---|---|
| Sign-ups per client address | `RETICULA_SIGNUP_RATE`, default `5/hour` |
| Client registrations per client address | `RETICULA_REGISTER_RATE`, default `20/hour` |
| Failed sign-ins for one e-mail address from one client address | 6 in 15 minutes |
| Failed sign-ins from one client address | 30 in 15 minutes |
| Writes per account (`put`, `edit`, `link`, `share`, `delete`) | `RETICULA_WRITE_RATE`, default `600/hour` |
| Time for one database statement of a tool call | 15 seconds, then the tool answers `Unavailable` |
| Password length | at least 10 characters |

The limits per client address depend on `RETICULA_TRUSTED_PROXIES`, as described above. Over the write limit a tool answers `RateLimited` and says when to try again. Expired tokens and authorization codes are deleted 3 days after they expire.

## Logs

```
docker compose logs app
```

The server writes one line per request. Two lines from a test stack:

```
app-1  | 2026-11-25T13:13:48Z POST /mcp 200 2ms from=172.31.89.1 agent="ExampleConnector/1.0" mcp=2025-06-18 client=c_IIQvKb2C
app-1  | 2026-11-25T13:13:49Z POST /token 400 3ms from=172.31.89.1 agent="ExampleConnector/1.0" reason="invalid_grant: credential is invalid, expired or revoked"
```

A line has the time in UTC, the method, the path without its query string, the status and the time until the answer began. Then:

| Field | What it is |
|---|---|
| `from=` | The caller's address. `X-Forwarded-For` is believed only from the peers in `RETICULA_TRUSTED_PROXIES`, as described under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host). |
| `agent=` | The name the calling program gives itself, its `User-Agent` header. |
| `mcp=` | The MCP protocol version the caller asked for, when it sent one. |
| `client=` | The first 10 characters of the connection's client id, when the request carried a valid token. All requests of one connection have the same value. `client=reticula-t` is a token minted with `reticula-mcp token`. |
| `reason=` | For a status of 400 and above, the server's own error text. |

Behind a proxy that is not in `RETICULA_TRUSTED_PROXIES`, `from=` is the proxy's address on every line. Measured with a Caddy container at `172.31.89.10` in front: with nothing trusted the lines had `from=172.31.89.10`, also for a request that sent an `X-Forwarded-For` of its own, and with that address trusted they had the address the proxy saw the caller come from. A process on the host reaches the server from the gateway address, so that is what `from=` shows for a tunnel on the host. With the gateway trusted, a request that carried `X-Forwarded-For: 203.0.113.7` was logged with `from=203.0.113.7`, and one without the header with the gateway address.

A request for `/healthz` that gets status 200 leaves no line, because the container asks every few seconds. A tool that fails answers with status 200 and its error as text. Its line has no reason: a `get` for a note that does not exist was logged as `POST /mcp 200`.

A line never holds a request body, a token, a note, or what a tool was asked and answered. After the runs for the next part (a sign-up, sign-ins, a wrong password, tokens, a renewal, and tool calls that wrote and searched a note) the log was searched for the passwords, the access and refresh tokens, the authorization codes, the `state` and PKCE values, the hidden values of the sign-in page, the e-mail address, the handle, and the title and text of the note. None of them was in it. Of the client id, which has 26 characters, it held the first 10.

The other lines come from the start: one `applied` line per migration or `schema is up to date`, four lines of the web server that begin with `INFO:`, and the lines about the embedding model.

### When an assistant does not connect

Follow the log while the assistant tries:

```
docker compose logs -f --since 1m app
```

Three questions, in this order.

**Did it reach the server at all?** Every request leaves a line, also a refused one and one for a path that does not exist (`GET /nothing-here 404`). No line at the time of the attempt means that the request never arrived. Then the cause is in front of the server: the name, the tunnel or proxy, or the port. Claude's custom connectors accept port 443 only, see [Tailscale Funnel](#tailscale-funnel). To send a request of your own through the public name, ask for `/.well-known/oauth-authorization-server`, not for `/healthz`.

**How far did the sign-in get?** A connector that connects for the first time, and the person who signs in for it, left these lines, in this order:

```
POST /mcp 401 0ms from=172.31.89.1 agent="ExampleConnector/1.0" reason="invalid_token: Authentication required"
GET /.well-known/oauth-protected-resource/mcp 200 0ms from=172.31.89.1 agent="ExampleConnector/1.0"
GET /.well-known/oauth-authorization-server 200 0ms from=172.31.89.1 agent="ExampleConnector/1.0"
POST /register 201 4ms from=172.31.89.1 agent="ExampleConnector/1.0"
GET /authorize 302 2ms from=172.31.89.1 agent="Mozilla/5.0 (example browser)"
GET /signin 200 1ms from=172.31.89.1 agent="Mozilla/5.0 (example browser)"
POST /signin 303 39ms from=172.31.89.1 agent="Mozilla/5.0 (example browser)"
POST /token 200 3ms from=172.31.89.1 agent="ExampleConnector/1.0"
POST /mcp 200 2ms from=172.31.89.1 agent="ExampleConnector/1.0" mcp=2025-06-18 client=c_IIQvKb2C
```

The time and the `app-1  |` prefix are left out here. Both programs ran on the host of the test stack, so `from=` is the gateway address on every line.

| Line | What happened |
|---|---|
| `POST /mcp 401` with `invalid_token` | The first call, without a token. This is normal: the answer tells the connector where to sign in. |
| the two `GET /.well-known/...` | The connector read where and how to sign in. |
| `POST /register 201` | The connector registered itself. |
| `GET /authorize 302`, `GET /signin 200` | A browser opened the sign-in page. `agent=` is now the browser, and `from=` the person's address. |
| `POST /signin 303` | The person signed in or signed up. The browser was sent back to the connector with a code. |
| `POST /token 200` | The connector collected its tokens with that code. |
| `POST /mcp 200` with `client=` | The connector calls with its token. |

The last of these lines that is in the log says how far it got. Three runs that were stopped on purpose:

- A connector that never registers. The log ends after the two `GET /.well-known/...` lines. The server answered everything it was asked, and the connector did not go on. The cause is in the connector: it does not register by itself, or it did not accept what it read.
- A sign-in that is finished in the browser, and a code that is never collected. The log ends with `POST /signin 303`, and no `POST /token` follows. The server sent the browser back to the connector's address with the code, and the connector never came for its tokens. Look at the page the browser showed after the sign-in.
- A token that has expired. The call is refused, the connector renews its token, and the next call works:

  ```
  POST /mcp 401 1ms from=172.31.89.1 agent="ExampleConnector/1.0" mcp=2025-06-18 reason="invalid_token: Authentication required"
  POST /token 200 4ms from=172.31.89.1 agent="ExampleConnector/1.0"
  POST /mcp 200 2ms from=172.31.89.1 agent="ExampleConnector/1.0" mcp=2025-06-18 client=c_IIQvKb2C
  ```

  The reason is the same for an expired token, a revoked token, a made-up token and no token. What follows tells them apart. When the renewal is refused too, the line is `POST /token 400` with `reason="invalid_grant: credential is invalid, expired or revoked"`. The connection is over then, and the assistant has to sign in again.

**What did the server answer?** The status and `reason=` say it. Lines from the test stack:

| Line | Meaning |
|---|---|
| `POST /signin 401` | The sign-in page refused the e-mail address and password. |
| `POST /signin 429`, no reason | The sign-up limit of that address was used up. See [Limits](#limits). |
| `POST /register 429`, `reason="temporarily_unavailable: too many registrations from this address; try again later"` | The registration limit of that address was used up. Behind a proxy that is not trusted, all callers share one address. |
| `POST /register 400`, `reason="invalid_redirect_uri: redirect URI contains forbidden characters"` | The server refused the connector's registration. The reason says which part. |
| `GET /authorize 400`, `reason="invalid_request: Client ID 'c_nosuchclient' not found"` | The connector used a registration that this server does not have, for example one from before a restore. |
| `POST /mcp 421`, no reason | The `Host` header was not the one of the public URL. The line before it in the log reads `Invalid Host header: reticula.test`. See the table of `Host` headers under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host). |
| `POST /mcp 403`, no reason | The request had an `Origin` header from another site. The line before it reads `Invalid Origin header: https://other.example.com`. |
| `POST /mcp 400`, reason begins with `Bad Request: Unsupported protocol version: 2099-01-01` | The caller asked for an MCP protocol version that the server does not speak. The reason lists the versions it does. |

## Upgrade

1. Back up the database (below).
2. Move the clone to the new release, and set the new `RETICULA_VERSION` in `.env`.

   ```
   git fetch --depth 1 origin tag v<release>
   git checkout v<release>
   ```

   For 0.1.1 the last line that `git` prints is `HEAD is now at e8ca1eb Release 0.1.1`. `.env` stays as it is. If you run a checkout and not a release, update the checkout.

3. Rebuild and restart.

   ```
   docker compose up -d --build --wait
   ```

4. Check `/healthz` and run the smoke test.

Step 2 moves the clone because the files under `deploy/` belong to a release. The compose file of 0.1.1 passes two settings to the server that the one of 0.1.0 does not pass: `RETICULA_EMBED_MODEL` and `RETICULA_ALLOW_NEWER_SCHEMA`. In the drill the compose file of 0.1.0 with `RETICULA_VERSION=0.1.1` started 0.1.1 and it was healthy, but the container had neither setting.

The server container applies pending migrations when it starts and logs one line per file. From 0.1.0 to 0.1.1:

```
app-1  | applied 0203_shared_rules.sql
app-1  | applied 0301_title_words.sql
```

0.1.1 adds these two migrations to those of 0.1.0:

- `0203_shared_rules` defines three SQL functions and changes no table.
- `0301_title_words` adds one index on the note table, over the words of each title. No column is added and no row is changed. The index is built at that start, before the server answers. On a test database with 30,041 notes the build took 36 to 83 milliseconds.

The drill went from 0.1.0 to 0.1.1, both installed from PyPI. Building the image of 0.1.1 took 22 seconds with no step taken from Docker's build cache. With the image built, the command of step 3 took 7 to 8 seconds. The server was away for one to two seconds: `/healthz` was asked every 0.1 seconds during the switches, and the longest time without an answer was 1.1 and 1.5 seconds in the two switches that applied both migrations, and 1.4 seconds in one that had nothing to apply.

Tokens issued before the upgrade worked after it: the access and refresh tokens of a sign-in, and a token minted with `reticula-mcp token`. An account signed in with the password it had before. The smoke test of 0.1.1 passed.

Notes written before the upgrade read back. Of 112 recorded replies of `start`, `profile`, `search`, `get` and `share`, 98 were the same before and after. The other 14 differed in one or two lines each, and all of those are lines that 0.1.1 words differently on purpose: who reads a hub through the hub above it, who has access to a hub that you can add to, and how the notes of a hub are counted. The release notes of 0.1.1 describe the three changes. No table other than the migration ledger had a different row count after the upgrade.

Migrations only move forward and each file runs in its own transaction. A migration file that has already been applied is never edited; changes arrive as new files. If the server reports that an applied file has changed, you are running mismatched code and database: do not continue.

The version-zero note format may still change. Read the release notes before you upgrade across a format change.

If the search index needs rebuilding after an upgrade, run `docker compose exec app reticula-mcp reindex`. The index holds only derived data, so rebuilding it changes no notes.

### Go back to the older version

The supported way back is the restore: move the clone back to the old release with `git checkout v<old release>`, set the old `RETICULA_VERSION`, and restore the dump from step 1 as described under Restore. Everything written after that dump is lost.

The old `RETICULA_VERSION` alone, without a restore, puts the older version on a database that the newer one has migrated. The older version does not know what those migrations changed. From 0.1.1 on the server refuses to start there. `docker compose up -d --build --wait` then ends, in the drill after 4 to 20 seconds, with

```
container reticula-app-1 is unhealthy
```

and `docker compose logs app` shows two lines for every attempt to start:

```
app-1  | [error:BadInput] the database has migrations this version does not have (0203_shared_rules): a newer version has used it
app-1  | hint: start that newer version again, or restore a dump taken before the upgrade. To run this version on the newer schema anyway, set RETICULA_ALLOW_NEWER_SCHEMA=1 (docs/operating.md, Upgrade)
```

The name in brackets is the migration this version has no file for. These lines are from the drill, where a build without the file of migration 0203 stood in for the older version. The server refuses before it applies anything; the migration ledger was the same afterwards. Docker starts the container again and again, with a pause that grows each time: 9 attempts in the first 31 seconds, the last one 13 seconds after the one before. Each attempt adds the same two lines to the log. Between the attempts `docker compose ps` shows the status `Restarting (1)`. The port does not answer.

The other commands refuse in the same way. On the refused stack `docker compose run --rm app reticula-mcp token --email someone@example.com --label laptop` printed the same two lines without the `app-1  |` prefix, minted no token and ended with status 1. Use `docker compose run --rm app` there and not `docker compose exec app`: the container that `exec` needs keeps restarting, and in the drill `exec` printed nothing and ended with status 137.

There are three ways out:

- Start the newer version again: set its `RETICULA_VERSION` and repeat step 3.
- Restore the dump, as above.
- Set `RETICULA_ALLOW_NEWER_SCHEMA=1` in `.env` and repeat step 3.

With `RETICULA_ALLOW_NEWER_SCHEMA=1` the older version starts on the newer schema and checks nothing further. Its log has one more line:

```
app-1  | warning: the database has migrations this version does not have (0203_shared_rules): a newer version has used it; running on the newer schema because RETICULA_ALLOW_NEWER_SCHEMA is set
```

A command such as `reticula-mcp token` prints the same line and then does its work.

That is safe only when the release notes of the newer version say that its migrations leave the columns and the rows of every table as they are. Otherwise the older version reads and writes tables in a form it does not know. Take the variable out of `.env` again once the newer version runs. While it is set, nothing is refused.

0.1.0 has no such check: it starts on a newer schema and says nothing. So 0.1.0 starts on a database that 0.1.1 has migrated, with the two migrations listed under Upgrade.

In the drill 0.1.0 from PyPI ran on a database that 0.1.1 from PyPI had migrated, with 0203 and 0301 in the ledger. It gave the same 112 replies as 0.1.0 on its own schema with the same data: the replies were recorded under 0.1.0, the stack went to 0.1.1 and back with nothing written in between, and none of the 112 differed afterwards. Its smoke test passed there. Notes written under 0.1.1 read back under 0.1.0, tokens issued under 0.1.1 worked, and a link written under 0.1.0 on the migrated database was there under 0.1.1 again. The switch back left the server without an answer for 1.5 seconds. That is a measurement for 0.1.0 and these two migrations, not a promise for any other pair of versions.

## Back up

The whole instance is one PostgreSQL database. Dump it while the server runs:

```
docker compose exec -T db pg_dump -U reticula --format=custom reticula > reticula-$(date +%Y%m%d).dump
```

The dump contains every note, the password hashes and the private keys of every account, unencrypted. Store it as carefully as the database itself, and off the machine that runs the database. Run the command from cron or a systemd timer. Keep a copy of `.env` with it: the dump does not contain the settings.

The embedding model is not in the dump. It is downloaded again when needed.

## Restore

Restore into an empty database. The steps below replace the data of an existing stack. Everything written after the dump was taken is lost. On a new machine, start at step 2 with the same `.env`.

The volume name in step 1 starts with the project name, which the compose file fixes to `reticula`. For a copy that was started under another project name, the volume is `<name>_reticula-db`, and every command below needs the same `-p <name>` or `COMPOSE_PROJECT_NAME`. Without it the commands act on the stack called `reticula`.

1. Stop the stack and remove the database volume. This deletes the current data.

   ```
   docker compose down
   docker volume rm reticula_reticula-db
   ```

2. Start the database only. If the server starts first, it creates its tables and the restore fails on them.

   ```
   docker compose up -d --wait db
   ```

3. Restore the dump.

   ```
   docker compose exec -T db pg_restore -U reticula --no-owner --exit-on-error --dbname reticula < reticula-20261125.dump
   ```

4. Start the server. If the dump is older than the installed version, the server brings the schema up to date. If the dump is from a newer version, the server refuses to start, as described under "Go back to the older version".

   ```
   docker compose up -d --build --wait
   ```

5. Check `/healthz` and run the smoke test.

In the drill the restored instance had the same number of rows in every table as the original. `docker compose exec app reticula-mcp rebuild` printed the same counts on both. Notes read back, shared and private. Accounts keep their person IDs, keys and tokens, so connected assistants keep working, as long as `RETICULA_PUBLIC_URL` is the same as before. With a different public URL, tokens issued at sign-in got status 401 and tokens minted with `reticula-mcp token` still worked.

The same steps took an instance back after an upgrade. A dump taken under 0.1.0 was restored after the upgrade to 0.1.1, with `RETICULA_VERSION=0.1.0` and the compose file of 0.1.0: steps 1 to 3 took 15 seconds and step 4 another 9. Every table had the row counts from before the upgrade, 112 recorded replies were the same as before it, and the notes written after the dump were gone. Started on that database again, 0.1.1 applied its two migrations as in the first upgrade.

## Accounts, tokens and connections

The commands run inside the server container. They read the database directly. Type them in `deploy/`, like every `docker compose` command of this guide.

Create an account without the sign-in page. This also works when sign-ups are closed. `--handle` is optional.

```
docker compose exec app reticula-mcp account create --email someone@example.com --handle someone
```

It asks `Password:` and `Again:`, shows nothing while you type, and answers `created @someone` with the person ID. The password needs at least 10 characters. The e-mail address is not verified: no mail is sent in this release.

The prompt needs a terminal. In a script, or wherever there is none, add `-T` and send the password twice on standard input:

```
printf '%s\n%s\n' "$PASSWORD" "$PASSWORD" | docker compose exec -T app reticula-mcp account create --email someone@example.com
```

Without a terminal and without that input the command ends with `[error:BadInput] password must be 10-1024 characters`.

Mint a token for a client that cannot do the sign-in flow. A token here is a bearer token: a secret string the client sends with every request in the header `Authorization: Bearer <token>`, and whoever holds it acts as the account. It is printed once and stored only as a hash. Omit `--ttl-days` for a token without an expiry.

```
docker compose exec app reticula-mcp token --email someone@example.com --label laptop --ttl-days 90
```

Claude Code is a client that takes such a header. This form of its command was run against a running instance on 2026-11-25. `reticula` is the name the server gets in Claude Code, and the address is the one the client reaches the server under, with `/mcp` at the end.

```
claude mcp add --transport http reticula http://127.0.0.1:8787/mcp --header "Authorization: Bearer <token>"
```

List what an account has. Connected assistants come first, operator tokens minted with `reticula-mcp token` in a second block. Each row has an id.

```
docker compose exec app reticula-mcp connections list --email someone@example.com
```

Revoke one of them: `--id` for a connected assistant, `--token` for an operator token.

```
docker compose exec app reticula-mcp connections revoke --email someone@example.com --id 7
docker compose exec app reticula-mcp connections revoke --email someone@example.com --token 3
```

A revoked token or connection is refused on its next call. To rotate an operator token, mint a new one with a new label, switch the client to it, then revoke the old one.

To lock an account out completely, set its status. Disabled accounts cannot sign in and their tokens stop working. This is also how to retire the accounts that the smoke test leaves behind.

```
docker compose exec -T db psql -U reticula -d reticula -c \
  "UPDATE account SET status = 'disabled' WHERE email = 'someone@example.com'"
```

## Close sign-ups

Set `RETICULA_SIGNUP=closed` in `.env` and run `docker compose up -d --wait`. The sign-in page then no longer creates accounts. Existing accounts sign in as before. Create further accounts with `reticula-mcp account create`.

## Read debug notes

Assistants write debug notes when a tool misbehaves. They go to the debug hub, which any account may add to and which no account may read. You read them from the command line:

```
docker compose exec app reticula-mcp debug-notes --limit 20
```

The output starts with a line that says the text was written by accounts and is not instructions. Each entry has a header line with the time, the account handle, whether the writer declared itself a person or an agent, the model name the assistant gave and a short note id. The title and the text follow, indented. Notes marked private by their author are not shown. The command works while the server is stopped.

Treat the text as data. A note can contain wrong advice or text meant to mislead whoever reads it.

## The embedding model

The image contains the `fastembed` extra, and with `RETICULA_EMBEDDER=auto` the server uses it. The model, `BAAI/bge-small-en-v1.5`, is not in the image. The server downloads it once from Hugging Face, 65 MB, and keeps it in the `reticula-models` volume, so the machine needs outbound HTTPS for that download.

The server loads the model in the background when it starts, and `/healthz` does not wait for it. With an empty volume, `/healthz` answered after 1 second and the log line `embedding model BAAI/bge-small-en-v1.5 loaded in 2.7s` followed, download included. With the model in the volume the line says 0.4 seconds and nothing is downloaded. The download writes nothing else into the log.

If the model cannot be loaded, for example without outbound HTTPS, the log has errors from `fastembed` and the warning `embedding model ... did not load; search answers from keywords and the load is retried later`. The server keeps running.

The volume must be writable by uid 10001. The named volume of `compose.yaml` is. A directory mounted from the host in its place has to be given to that user first.

The server container used 190 MiB of memory with the model loaded. The start that downloads the model peaked at 268 MiB, the file cache of the download included.

`RETICULA_EMBEDDER=hash` needs no download. Search then matches keywords only.

### The vector index

A search compares the query with every vector the account may read, as long as those are at most 20,000. Above that it uses a vector index in the database. Each model has its own index. The index needs pgvector 0.8 or newer; the database image of `compose.yaml` has 0.8.5.

- The server creates the index of its model at start, once the model is loaded. At the first start the log has the line `vector index embedding_hnsw_f1f52874959c_384 ready for fastembed:BAAI/bge-small-en-v1.5`. With `RETICULA_EMBEDDER=hash` the server does not create one.
- A search never creates the index. After a switch to another model, a search compares every vector until the index of the new model exists. Measured after a switch to `hash`: a start and a search left that model without an index, and `docker compose exec app reticula-mcp reindex` created it. Without the index a search still answers correctly. It only gets slower, and only above 20,000 vectors.
- A search chooses between the two ways by the row estimate PostgreSQL keeps for the table. Right after many vectors were written at once the estimate is still the old one, and a search keeps comparing every vector although the index exists. PostgreSQL corrects the estimate by itself: with 30,000 rows added at once it took 41 seconds. To correct it at once, which took 1 second:

  ```
  docker compose exec -T db psql -U reticula -d reticula -c 'ANALYZE embedding'
  ```

### Change the model

`RETICULA_EMBED_MODEL` in `.env` names another model of `fastembed`. Apply it with `docker compose up -d --wait`. Measured with `sentence-transformers/all-MiniLM-L6-v2` and with `jinaai/jina-embeddings-v2-small-en`, which has another vector size:

- The server downloaded the model and was healthy after 7 seconds. The log names the model: `embedding model sentence-transformers/all-MiniLM-L6-v2 loaded in 2.6s`, then `vector index embedding_hnsw_40541eb5df3b_384 ready for fastembed:sentence-transformers/all-MiniLM-L6-v2`.
- The server then computes the vectors of all notes under the new model by itself, in the background. All 6 notes of the test instance had theirs 7 seconds after the switch, with no command run. Search works in the meantime.
- `docker compose exec app reticula-mcp reindex` does the same work in one run and returns when it is done. Run it on a large instance to know when the new model is complete. After a switch to `RETICULA_EMBEDDER=hash` it is needed: the server does not create that index by itself.
- The vectors and the index of the old model stay in the database, and its files stay in the volume. After a switch back they were used again at once. `docker compose exec app reticula-mcp reindex --drop` removed the vectors and the index of the old model. It does not remove model files from the volume.
- A name `fastembed` does not know: the server starts and stays healthy, and the log has `embedding model fastembed:no/such-model did not load; search answers from keywords; next try in 30s` with `ValueError: Model no/such-model is not supported in TextEmbedding`. Saving and searching still worked.

## What the container does

- The server runs as a user without root rights (uid 10001).
- At start it waits for the database, up to `RETICULA_DB_WAIT` seconds (default 120). If the database does not come, the container exits and the restart policy starts it again. Started with the database down, it was healthy 3 seconds after the database came up.
- The health check asks `/healthz` every 10 seconds.
- On `docker compose stop` or `restart` the server shuts down within a second.
- The server keeps no session state (`RETICULA_STATELESS_HTTP=true`). A restart or a recreated container does not disconnect assistants: an open session and its token kept working across both.
- Both containers have the restart policy `unless-stopped`, so the stack comes back after a reboot when Docker itself starts at boot.

## What lives where

| Thing | Where |
|---|---|
| All notes, hubs, grants, accounts, keys, tokens | the PostgreSQL database, in the volume `reticula_reticula-db` |
| Search index (embeddings) | a table in the same database, rebuildable with `reindex` |
| Embedding model | the volume `reticula_reticula-models`, downloaded again when missing |
| Settings and the database password | `deploy/.env`, which git ignores |
| Shipped instruction texts | inside the installed package; copied into the database when migrations run |

## Without Docker

The server is one command and needs a PostgreSQL database with the pgvector extension. Set `RETICULA_DATABASE_URL` and the other settings in the environment. With `uvx`, write `uvx reticula-mcp ...` where this page writes `reticula-mcp ...`.

```
reticula-mcp migrate
reticula-mcp serve
```

`serve` refuses to start when migrations are pending. `serve --migrate` applies them first. `serve --host H --port P` overrides `RETICULA_HOST` and `RETICULA_PORT`. The defaults are `127.0.0.1` and `8787`. Run the server under a process manager so it comes back after a reboot.

`migrate` is safe to run any number of times. Besides applying schema files, it creates the system account (handle `reticula`, no password, cannot sign in) and loads the shipped instructions. The instructions are written as notes by the system account, in a hub that every account can read. It also creates the debug hub. A second run changes nothing. A new version of a shipped note is written only when its text changed in the release you installed.

The other commands on this page are the same without the `docker compose exec app` prefix. Back up with `pg_dump --format=custom "$RETICULA_DATABASE_URL"`, and restore with `pg_restore --no-owner --exit-on-error` into an empty database before the first `migrate`.

A tunnel or proxy on the same host reaches a server that runs as a host process from `127.0.0.1`. That is then the value for `RETICULA_TRUSTED_PROXIES`.
