# Operating a Reticula MCP instance

This page is for the person who runs the server. Settings are listed in the [README](../README.md).

The commands are written for the Docker Compose files in [`deploy/`](../deploy) and are run from that directory. They were run as written here against a throwaway copy of the stack, with Docker Compose 5.8 on macOS, where Docker runs inside a Linux virtual machine. On Linux, the project's Check run on GitHub builds the image from the checkout, starts the stack of `compose.dev.yaml` and runs the smoke test against it on every change. On 2026-11-25 that was Ubuntu 26.04 with Docker Compose 5.1.3. No older version of Compose was tried. To run the server without Docker, see [Without Docker](#without-docker) at the end.

## Start

1. Get the files. They are in the repository on GitHub; the package on PyPI does not contain them. Clone the repository at the tag of the release you want to run, and go to its `deploy` directory.

   ```
   git clone --branch v<release> --depth 1 https://github.com/retospect/reticula-mcp.git
   cd reticula-mcp/deploy
   ```

   For release 0.2.0 the option is `--branch v0.2.0`. The releases are listed on [PyPI](https://pypi.org/project/reticula-mcp/#history). `git` prints a warning that the tag is not a commit and a note about a detached HEAD. Both are expected.

   The files under `deploy/` at a tag belong to that release, and so does this guide. Read it at the same tag: the main branch can describe things a release does not have yet.

2. Create the settings file.

   ```
   cp .env.example .env
   chmod 600 .env
   ```

   The file will hold the database passwords, so only your own user may read it. Set three values in `.env`:

   - `RETICULA_DB_PASSWORD`: the password of the role `reticula`, which the server connects as. Generate one with `openssl rand -hex 24`.
   - `RETICULA_DB_ADMIN_PASSWORD`: the password of the database superuser `reticula_admin`, which only you use and the server never sees. Generate another one the same way.
   - `RETICULA_VERSION`: the release you cloned, without the `v`. For the tag `v0.2.0` it is `0.2.0`.

   Leave `RETICULA_PUBLIC_URL` and `RETICULA_TRUSTED_PROXIES` empty for now.

   If port 8787 is taken on your machine, set `RETICULA_PORT` to a free one. It is the port published on `127.0.0.1`. Write it in place of 8787 in every command of this guide. Inside the container the server stays on 8787, and its log names that port.

   Make the key file, the secret that encrypts the private keys of the accounts in the database:

   ```
   mkdir -p secrets
   (umask 077; openssl rand -base64 32 > secrets/reticula.key)
   ```

   With Docker on a Mac (Docker Desktop or colima) that is all: the file has mode 600 and belongs to you. On Linux the server's user in the container must own it:

   ```
   sudo chown 10001:10001 secrets/reticula.key
   ```

   Keep a copy of this file now, apart from the backups of the database: [The key file](#the-key-file) says why and what happens without it.

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

   The answer is `{"status":"ok","version":"0.2.0"}`, with the installed version, and `200` on the next line. When the database does not answer, it is `{"status":"unavailable","version":"0.2.0"}` and `503`. `docker compose ps` shows the same result as `healthy` or `unhealthy`.

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

- Ports. A port other than 443 works only for assistants that connect from your own machines, such as Claude Code. Claude's custom connectors (claude.ai, the desktop and mobile apps) connect from Anthropic's servers and accept port 443 only: "Claude can't connect to a URL that specifies another port" (Claude support, "Get started with custom connectors using remote MCP", read on 2026-11-25). For those, run Reticula on a machine whose port 443 is free, or give it a name of its own: a Tailscale node in a container, under [Tailscale Funnel from a container](#tailscale-funnel-from-a-container), or a reverse proxy. On a machine that already uses Funnel on 443, a second service goes on 8443 or 10000, and `RETICULA_PUBLIC_URL` must then carry that port.
- Client addresses. Measured: in its HTTPS mode Funnel writes `X-Forwarded-For` itself and replaces a value sent by the client. A request with a made-up header was counted under the caller's real address. With `tailscaled` on the host this is the first row of the settings table under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host): trust the gateway address. Its TCP modes add no header (read from the Tailscale source, not measured): leave `RETICULA_TRUSTED_PROXIES` empty for those.
- The `Host` header. Measured: Funnel passes the header on with its port, which is what `/mcp` expects when `RETICULA_PUBLIC_URL` carries the port.
- Turning it off. To stop one tunnel, run its command again with `off` at the end. Measured: a tunnel started with `sudo` needs `sudo` to turn off as well, for example `sudo tailscale funnel --https=8443 off`. After `sudo tailscale set --operator=$USER` once, your own user can change the configuration without `sudo` (that form was not run). `tailscale funnel reset` clears the whole Funnel configuration of the machine, not one tunnel (both Tailscale documentation).
- `tailscale serve` is the same tunnel for the machines of your tailnet only, without the public internet. Tailscale's pages on Serve do not say whether it writes `X-Forwarded-For`, and it was not measured here. Set `RETICULA_TRUSTED_PROXIES` to the gateway address, run [Check the setting](#check-the-setting), and keep the value only if the table there says it is correct.

### Tailscale Funnel from a container

When port 443 of the machine is taken, [`compose.tailscale.yaml`](../deploy/compose.tailscale.yaml) adds a Tailscale node of its own in a container beside the server. The node has its own name, `<name>.<tailnet>.ts.net`, and its own port 443, which is what Claude's custom connectors need. Funnel on that node forwards to the server over the compose network. Nothing is published on the host, and Tailscale on the host is not needed and not touched.

The container runs `tailscale/tailscale:v1.104.7` with userspace networking: no `NET_ADMIN`, no `/dev/net/tun`, no published port. Its identity is kept in the volume `reticula-tailscale`. The serve configuration is [`tailscale/serve.json`](../deploy/tailscale/serve.json): HTTPS on 443, everything proxied to `http://app:8787`, Funnel on.

"Measured" below means one of two things. On the running instance of 2026-11-25: the whole list, from the sign-in to requests from outside the tailnet. On a throwaway stack without a sign-in: the container up and waiting, its restarts, and the files. "Tailscale documentation" was read on 2026-11-25 and not tried. "Tailscale source" is the code of version 1.104.7.

1. In the admin console (Tailscale documentation, except where marked):

   - MagicDNS on, and HTTPS certificates on: on the DNS page, "Enable HTTPS" under "HTTPS Certificates". Machine names and the tailnet's DNS name are then published in the public Certificate Transparency ledger, so choose a name that gives nothing away.
   - The `funnel` node attribute for the new node, in the policy file on the "Access controls" page. By default it targets `autogroup:member`. A node signed in with a tag is not a member. Measured: such a node got no Funnel until its tag was added to the target, and the permission arrived within seconds of saving.

   ```
   "nodeAttrs": [
     { "target": ["autogroup:member", "tag:server"], "attr": ["funnel"] }
   ]
   ```

2. Add to `.env`:

   ```
   COMPOSE_FILE=compose.yaml:compose.tailscale.yaml
   TS_HOSTNAME=<name>
   TS_EXTRA_ARGS=--advertise-tags=tag:server
   ```

   `COMPOSE_FILE` makes every `docker compose` command of this guide include the Tailscale container. Measured with `docker compose config`: the services are `db`, `app` and `tailscale`, and without the line `db` and `app`. `TS_HOSTNAME` is the first part of the public name. Use the name the admin console shows for the node in the steps below.

   The tag is optional. Use one that your policy file already has. Tailscale documentation: a tagged node has key expiry disabled by default. An untagged node's key expires after 180 days by default, and its connections then stop. On the "Machines" page, the menu of the node has "Disable Key Expiry".

3. Start the container. This leaves the server container as it is (measured).

   ```
   docker compose up -d tailscale
   docker compose logs tailscale
   ```

   The log has a link after the line `To authenticate, visit:`. Open it and sign in. Measured: the sign-in worked the first time. A start that gets no sign-in ends after 15 minutes (`TS_BOOT_TIMEOUT` in the file), and the container starts again with a new link: 24 times in 6 hours on the throwaway stack. With the image's default of 60 seconds there was a new link every minute.

   Instead of the link, an auth key can be put in `.env` as `TS_AUTHKEY`. It is used for the first sign-in only. This was not run. Tailscale documentation: keys are made on the "Keys" page with "Generate auth key" and last at most 90 days, and a node stays signed in after its key expires. Do not use an ephemeral key: such a node comes back as a new node after a restart.

   Measured on the throwaway stack: the identity survives `docker compose up -d --force-recreate tailscale`, because it lives in the volume. `docker compose down -v` deletes it, and the database with it.

4. Check that the node may use Funnel.

   ```
   docker compose exec tailscale tailscale status --json
   ```

   The output must have the key `funnel` under `Self`, `CapMap`. Measured: without the node attribute, `tailscale funnel status` in the container still said "Funnel on", and the public name had no DNS record. Tailscale documentation: a new public DNS record can take up to 10 minutes.

5. Set the public URL and trust the container. Every assistant that is connected has to be added again after this step: the server answers for the new name only. Measured: Claude Code reported that the protected resource does not match, at once.

   ```
   RETICULA_PUBLIC_URL=https://<name>.<tailnet>.ts.net
   RETICULA_TRUSTED_PROXIES=172.31.88.250
   ```

   `172.31.88.250` is the fixed address of the Tailscale container. With another `RETICULA_SUBNET`, set `RETICULA_TAILSCALE_ADDRESS` in `.env` to an address high in that subnet, and trust that address instead. Then apply:

   ```
   docker compose up -d --wait
   ```

6. Check the result, from a machine outside the tailnet.

   ```
   curl -s https://<name>.<tailnet>.ts.net/healthz
   ../scripts/smoke https://<name>.<tailnet>.ts.net
   ```

   Measured: `/healthz` answered with status 200 and a valid certificate, and `/mcp` without a token with status 401. The smoke test was not run through the node yet. It must print 14 lines that start with `ok`, and the `authorize` line must end with `cookie Secure`. It signs up a throwaway account, so with sign-ups closed give it an existing account with `--email` and the password in `SMOKE_PASSWORD`. Then run [Check the setting](#check-the-setting). Measured: before the container was trusted, the request log said `from=172.31.88.250` for every request. After, it gave the caller's public address. Tailscale source: the proxy replaces `X-Forwarded-For` with the address the request came from, and passes the `Host` header on as the client sent it.

To take the public name down, run `docker compose stop tailscale`. This was not run. The node's identity stays in the volume for the next `docker compose up -d`.

## Limits

| Limit | Setting or value |
|---|---|
| Sign-ups per client address | `RETICULA_SIGNUP_RATE`, default `5/hour` |
| Client registrations per client address | `RETICULA_REGISTER_RATE`, default `20/hour` |
| Failed sign-ins for one e-mail address from one client address | 6 in 15 minutes |
| Failed sign-ins from one client address | 30 in 15 minutes |
| Writes per account (`put`, `edit`, `link`, `share`, `delete`) | `RETICULA_WRITE_RATE`, default `600/hour` |
| Reads per account (`start`, `profile`, `search`, `get`, `share` without a person) | `RETICULA_READ_RATE`, default `300/minute` |
| Reads of one account at once | `RETICULA_READ_CONCURRENCY`, default `4` |
| Time for one read | `RETICULA_READ_SECONDS`, default `20` seconds, then the tool answers `Unavailable` |
| Time for one database statement of a tool call | 15 seconds, then the tool answers `Unavailable` |
| Password length | at least 10 characters |

The limits per client address depend on `RETICULA_TRUSTED_PROXIES`, as described above. Over the write limit a tool answers `RateLimited` and says when to try again. The read limits answer the same way: `read limit reached (300/minute); try again in 12 s`, or, when the account already has as many reads running as `RETICULA_READ_CONCURRENCY` allows and none ends within 10 seconds, `read limit reached (4 at once); try again in a moment`. A waiting read holds no database connection. The read limits are kept in the server's memory, per process, and start again empty when it restarts; `0` switches one off. Expired tokens and authorization codes are deleted 3 days after they expire.

## Logs

```
docker compose logs app
```

The server writes one line per request. Two lines from a test stack:

```
app-1  | 2026-11-25T13:13:48Z POST /mcp 200 2ms from=172.31.89.1 agent="ExampleConnector/1.0" mcp=2025-06-18 client=c_IIQvKb2C
app-1  | 2026-11-25T13:13:49Z POST /token 400 3ms from=172.31.89.1 agent="ExampleConnector/1.0" reason="invalid_grant: credential is invalid, expired or revoked"
```

A line has the time in UTC, the method, the path without its query string, the status and the time until the answer began. A path that holds a space, a quote or a backslash after its percent signs are decoded is written in double quotes, as `agent=` is: `GET "/no such page" 404`. Then:

| Field | What it is |
|---|---|
| `from=` | The caller's address. `X-Forwarded-For` is believed only from the peers in `RETICULA_TRUSTED_PROXIES`, as described under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host). |
| `agent=` | The name the calling program gives itself, its `User-Agent` header. |
| `mcp=` | The MCP protocol version the caller asked for, when it sent one. |
| `client=` | The first 10 characters of the connection's client id, when the request carried a valid token. All requests of one connection have the same value. `client=reticula-t` is a token minted with `reticula-mcp token`. From 0.2.0 on, `reticula-mcp connections list` shows the same value in its `client` column. |
| `reason=` | For a status of 400 and above, the server's own error text. For the sign-in page it is a fixed word such as `signin: bad_credentials`. |

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
| `POST /signin 401`, `reason="signin: bad_credentials"` | The sign-in page refused the e-mail address and password. |
| `POST /signin 429`, `reason="signin: throttled"` | Too many failed sign-ins. In the test the seventh wrong password for one e-mail address got this line. See [Limits](#limits). |
| `POST /signin 429`, `reason="signin: signup_rate"` | The sign-up limit of that address was used up. See [Limits](#limits). |
| `POST /signin 403`, `reason="signin: closed"` | A sign-up was sent while `RETICULA_SIGNUP=closed`. The sign-up page has no form then, so a browser that loaded the page after the change does not send one. |
| `POST /signin 400`, `reason="signin: link_gone"` | The form of a sign-in that was already finished was sent again, or the link had expired. The person has to start again from the assistant. |
| `POST /signin 403`, `reason="signin: page_expired"` | The hidden check value of the form was wrong. The page asks the person to load it again. |
| `POST /signin 400`, `reason="signin: password_length"` | A sign-up with a password of 3 characters. |
| `POST /signin 409`, `reason="signin: Conflict"` | A sign-up with an e-mail address that already has an account. |
| `POST /signin 400`, `reason="signin: BadInput"` | A sign-up with a handle that is not allowed, here one with a space. |
| `POST /register 429`, `reason="temporarily_unavailable: too many registrations from this address; try again later"` | The registration limit of that address was used up. Behind a proxy that is not trusted, all callers share one address. |
| `POST /register 400`, `reason="invalid_redirect_uri: redirect URI contains forbidden characters"` | The server refused the connector's registration. The reason says which part. |
| `GET /authorize 400`, `reason="invalid_request: Client ID 'c_nosuchclient' not found"` | The connector used a registration that this server does not have, for example one from before a restore. |
| `POST /mcp 421`, `reason="Invalid Host header"` | The `Host` header was not the one of the public URL. The line before it in the log has the header as the caller sent it: `2026-11-25T14:02:23Z Invalid Host header: other.example.com`. See the table of `Host` headers under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host). |
| `POST /mcp 403`, `reason="Invalid Origin header"` | The request had an `Origin` header from another site. The line before it reads `2026-11-25T14:02:23Z Invalid Origin header: https://other.example.com`. |
| `POST /mcp 400`, reason begins with `Bad Request: Unsupported protocol version: 2099-01-01` | The caller asked for an MCP protocol version that the server does not speak. The reason lists the versions it does. |

The reasons of the `/signin` lines and of the `421` and `403` lines are written from 0.2.0 on. 0.1.1 writes those lines without `reason=`, and the line before a `421` or `403` without the time.

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

0.2.0 adds no migration to those of 0.1.1. The files under `deploy/` are the same in both releases, except for the version in `.env.example`.

The drill went from 0.1.0 to 0.1.1, both installed from PyPI. Building the image of 0.1.1 took 22 seconds with no step taken from Docker's build cache. With the image built, the command of step 3 took 7 to 8 seconds. The server was away for one to two seconds: `/healthz` was asked every 0.1 seconds during the switches, and the longest time without an answer was 1.1 and 1.5 seconds in the two switches that applied both migrations, and 1.4 seconds in one that had nothing to apply.

Tokens issued before the upgrade worked after it: the access and refresh tokens of a sign-in, and a token minted with `reticula-mcp token`. An account signed in with the password it had before. The smoke test of 0.1.1 passed.

Notes written before the upgrade read back. Of 112 recorded replies of `start`, `profile`, `search`, `get` and `share`, 98 were the same before and after. The other 14 differed in one or two lines each, and all of those are lines that 0.1.1 words differently on purpose: who reads a hub through the hub above it, who has access to a hub that you can add to, and how the notes of a hub are counted. The release notes of 0.1.1 describe the three changes. No table other than the migration ledger had a different row count after the upgrade.

A second drill went from 0.1.1 to 0.2.0, both installed from PyPI. The server logged no `applied` line and the ledger stayed at 14 migrations. Building the image of 0.2.0 took 20 seconds with no step taken from Docker's build cache. The command of step 3 took 7 seconds with the first steps of the image taken from the cache, and `/healthz` was without an answer for 1.4 and 1.3 seconds in two such switches. Every table had the row counts from before. Tokens issued under 0.1.1 worked, a refresh token among them, and the smoke test of 0.2.0 passed. Of the 112 replies 83 were the same. The other 29 are searches with one result: their first line says `1 result` where 0.1.1 says `1 results`.

0.3.0 adds eight migrations to those of 0.2.0:

```
applied 0204_readable_hubs.sql
applied 0205_current_among.sql
applied 0302_embedding_backlog.sql
applied 0303_backlog_current_model.sql
applied 0350_recent_events.sql
applied 0351_onboarding.sql
applied 0352_word_handles.sql
applied 0353_readable_if_live.sql
```

- `0204`, `0205`, `0303` and `0353` add or redefine SQL functions. No table, index or row changes.
- `0302_embedding_backlog` adds two tables: `embedding_backlog`, the notes that still wait for a vector, and `embedding_complete`, the model the server last finished embedding.
- `0350_recent_events` adds three indexes, for `search(since=...)`.
- `0351_onboarding` adds the table `onboarding_shown`, one row for each connection that `start` has pointed to the instruction on using Reticula as memory.
- `0352_word_handles` adds the tables `account_setting` and `connection_setting`, which `edit(ref="settings", ...)` writes, and one function and one index.

0.3.0 also changes the default model from `BAAI/bge-small-en-v1.5` to `BAAI/bge-base-en-v1.5`, in `compose.yaml` and `.env.example`. A `.env` that sets `RETICULA_EMBED_MODEL` keeps its model. Otherwise the first start of 0.3.0 downloads bge-base and computes a vector of it for every note. [Change the model](#change-the-model) says what happens during such a switch.

This upgrade was rehearsed on a copy of a running 0.2.0 instance, restored from a dump into a throwaway stack: 3 accounts and 63 notes with bge-small vectors, 14 migrations in the ledger. The server was built from the release commit.

- `reticula-mcp migrate` applied the eight files above, in 1 second including the start of its container. The ledger then held 22 migrations. The same run added the new shipped instructions: the copy had 7 instruction notes afterwards, against 4 before.
- At the start `/healthz` answered after 1.3 seconds and the container was healthy after 5.8. The log had `embedding model BAAI/bge-base-en-v1.5 loaded in 4.4s`, the download included. The model takes 209 MB in the volume.
- Every one of the 66 notes had a bge-base vector 19.5 seconds after the container started, with no command run. The backlog was then empty.
- The server container used 515 MiB of memory while it embedded and 618 MiB afterwards. With bge-small it used 190 MiB in an earlier measurement, under [The embedding model](#the-embedding-model).
- The 63 bge-small vectors were still there. `docker compose exec app reticula-mcp reindex --drop-other-models` removed them in 0.5 seconds and answered `removed 63 vectors and 1 vector indexes of other models; fastembed:BAAI/bge-base-en-v1.5 is kept`. The database was 11 MB afterwards.
- With an account and two tokens made in the copy, through MCP: the first `start` of each token pointed to the instruction on using Reticula as memory, and no later `start` through the same token did. Two notes written then were found at once by their words and by their meaning, the meaning search sharing no word with the note. `get` of a hub and `search(query="", since="7d")` answered.

A second rehearsal on a fresh copy kept the connections made before the upgrade. Under 0.2.0 from PyPI, two accounts made in the copy connected through the sign-in page, and a third got a token from `reticula-mcp token`. Then 0.2.0 was stopped and 0.3.0 started on the same database, applying the eight migrations. Without a new sign-in, the minted token and the access tokens of both connections answered `start`. The refresh grant with the refresh token from 0.2.0 returned a new pair, and the new access token answered `start`. Nobody has to connect again.

Not tried in the rehearsals: the smoke test, and the way back to 0.2.0, which is the restore of the dump from step 1.

0.4.0 adds one migration to those of 0.3.0:

```
applied 0354_public_nodes.sql
```

It defines the SQL function `public_nodes()`, which the page `/hubs` reads. No table, index or row changes. The model and the settings are those of 0.3.0.

0.4.0 also serves public pages on the same port as the connector: `/`, `/slides`, `/hubs`, `/connect`, `/vision`, `/privacy`, `/results` and `/robots.txt`. They need no setting. `/connect` shows the connector address built from `RETICULA_PUBLIC_URL`. A proxy in front of the server that forwards only `/mcp` and the sign-in pages leaves the public pages unreachable, and that is a valid way to run it. `/hubs` lists nothing until an owner runs `share(hub=..., person="public", mode="read")`.

This upgrade was rehearsed the same way, on a copy of a running 0.3.0 instance: 3 accounts, 80 notes, 22 migrations. Under 0.3.0 from PyPI, two accounts made in the copy connected through the sign-in page and a third got a token from `reticula-mcp token`. Then 0.3.0 was stopped and the release candidate of 0.4.0 started on the same database.

- It was healthy 6.1 seconds after `docker compose up`, with `applied 0354_public_nodes.sql` as the only applied line.
- Without a new sign-in, the minted token and the access token of a connection answered `start`. The refresh grant with the refresh token from 0.3.0 returned a new pair, and its access token answered `start`.
- `/`, `/hubs`, `/connect`, `/results` and `/slides` answered with status 200 and a `Content-Security-Policy` header. `/hubs` said `No hub is public on this server yet`.
- After `share(hub=..., person="public", mode="read")` on a hub of one of those accounts, `/hubs` listed that hub by its title and short id, and nothing else of it: no address and no text. `/hubs` keeps a page for 60 seconds, so the change showed within a minute. After `mode="none"` the list was empty again.

0.4.1 adds no migration. Its `compose.yaml` sets limits that 0.4.0 does not have, and moving the clone in step 2 brings them in:

- Docker keeps the log of each container to 5 files of 10 MB, so a flood of requests cannot fill the disk that the database volume shares. `compose.tailscale.yaml` sets the same for its container.
- The server container may use at most 2 GB of memory and 512 processes. `RETICULA_APP_MEMORY` in `.env` sets another memory limit, for example `RETICULA_APP_MEMORY=4g`.

Measured on a copy of a running instance with 80 notes and the default model: a start with an empty model volume stayed under 0.7 GB and 52 processes. `docker compose exec app reticula-mcp reindex --drop` loads the model a second time in the same container and took the container to 1.3 GB, file cache included, and 78 processes. With `RETICULA_APP_MEMORY=1g` the start and that reindex still ran. With `512m` the server started, but the reindex ended after 1 second with status 137 and took the server container down with it, and Docker started it again. Give a larger model more: [Choose a model](#choose-a-model) lists the memory each model needs once loaded, and a reindex needs that twice.

0.4.2 adds one migration, `0355_private_key_form`, a check on the stored form of a private key. It needs a key file, and its server connects to the database as a role without superuser rights. Read before you upgrade to it:

- The private keys of the accounts are encrypted with the key file, and the server does not start while one is stored unencrypted.
- The database gets a superuser of its own, `reticula_admin`, and `reticula` becomes the owner of the database without superuser rights. On an install from before, `reticula` is the role the database was created with, and PostgreSQL does not let that role lose its rights. So the database is moved into a new volume by a dump and a restore.

The upgrade takes these steps in place of steps 3 and 4 above, after the backup and the move of the clone. Step 1 takes a dump; check it with `pg_restore -f /dev/null` as under [Check a backup](#check-a-backup) before step 2, which deletes the old database.

1. Make the key file, set `RETICULA_DB_ADMIN_PASSWORD` in `.env` (generate it with `openssl rand -hex 24`), build the new image while the old server runs, and take the dump:

   ```
   mkdir -p secrets
   (umask 077; openssl rand -base64 32 > secrets/reticula.key)
   docker compose build app
   (umask 077; docker compose exec -T db pg_dump -U reticula --format=custom reticula > reticula-before-0.4.2.dump)
   ```

   On Linux, `sudo chown 10001:10001 secrets/reticula.key` after the second line. Keep a copy of the key file apart from the backups: [The key file](#the-key-file).

2. Stop the stack and remove the database volume:

   ```
   docker compose down
   docker volume rm reticula_reticula-db
   ```

3. Start the new database, restore the dump as the role `reticula`, encrypt the keys and start the server:

   ```
   docker compose up -d --wait db
   docker compose exec -T db pg_restore -U reticula --no-owner --no-comments --single-transaction --dbname reticula < reticula-before-0.4.2.dump
   docker compose run --rm --no-deps app reticula-mcp key encrypt
   docker compose up -d --wait
   ```

   `key encrypt` prints how many keys it encrypted and may be run again: it changes only keys that are still unencrypted.

Keep the dump of step 1 until the upgrade is checked. The way back is that dump: move the clone back, and restore it with the steps under [Restore](#restore) of the old release.
- `reticula-mcp serve` does not start when `RETICULA_PUBLIC_URL` is an `https` address or names a host other than this machine, and `RETICULA_TRUSTED_PROXIES` is empty. Set it to the proxy's address, as under [A tunnel or proxy on the same host](#a-tunnel-or-proxy-on-the-same-host). `RETICULA_TRUSTED_PROXIES=none` starts the server as before, with every client in one rate-limit bucket. The refusal names the URL and what to set:

  ```
  [error:BadInput] RETICULA_PUBLIC_URL is https://notes.example.com, which is reached through a proxy, but RETICULA_TRUSTED_PROXIES is empty
  hint: set RETICULA_TRUSTED_PROXIES to the proxy's address (under docker compose, the gateway address of the compose network, 172.31.88.1). RETICULA_TRUSTED_PROXIES=none starts the server anyway, with every client in one rate-limit bucket
  ```

  Other commands, such as `migrate` and `token`, are not refused.
- In `compose.yaml` the database is on a network of its own that only the server joins, and that has no route out. The server and the database drop every Linux capability they do not need, and neither may gain privileges. The Tailscale container of `compose.tailscale.yaml` drops them all.
- The server gets the database password as `PGPASSWORD` and no longer inside the URL, so a password with characters such as `@`, `:`, `/` or `#` works.
- The image installs every dependency of the server from `deploy/requirements.txt`, which names each version and the hash of each file, and then the server itself without its dependencies. The base images of the server, the database and the Tailscale container are named by digest. A build therefore installs the same files each time, and a newer base image arrives only with a release.

Measured on a copy of a running 0.4.0 instance: `docker compose up -d --build --wait` with the new files created the network, recreated both containers and was healthy after 33 seconds, the image build included. The server container then had no capabilities at all, and the database container had the five its start needs (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`). The database could not reach the internet, and the server could, for the model download. A new stack with the database password `p@ss:w/rd?#%&x y` started and applied every migration. The Tailscale container ran without capabilities, printed its sign-in link, reached the server and could not reach the database. Its sign-in and Funnel were not tried.

The steps with the key file were rehearsed on a copy of a running 0.4.1 instance, with two connections made through the sign-in page and a token from `reticula-mcp token` under 0.4.1. The build took 19 seconds while the old server ran. `key encrypt` took 1 second and printed `encrypted 21 keys of 7 accounts; 0 were done already`, and a second run printed `encrypted 0 keys of 8 accounts; 24 were done already`. `docker compose up -d --wait` applied `0355_private_key_form.sql` and was healthy after 17 seconds, with the database container recreated for its new network. The server was away for 19 seconds in all. Without a new sign-in, the minted token and a connection's access token answered `start`, and the refresh token from 0.4.1 returned a new pair. A note written then was saved, and a new account got its keys encrypted. That rehearsal kept the old database; the steps above replace it.

The steps above were rehearsed on another copy of a running 0.4.1 instance, again with connections and a token made under 0.4.1. Step 1's dump took 0.3 seconds, removing the volume 1 second, the new database with its roles 6 seconds, the restore 0.2 seconds, `key encrypt` 1 second, and the last `up` 6 seconds. The server was away for 17 seconds in all. Every table had its rows from before, and the ledger one migration more. The minted token, a connection's access token and its refresh token from 0.4.1 worked without a new sign-in. `reticula` then had no superuser rights, owned the database and every table, and was refused `pg_read_file` and `CREATE ROLE`. The server container had neither database password of the superuser in its environment. A dump, a reindex and a write worked as before. The way back, the dump of step 1 restored into 0.4.1, was healthy after 14 seconds.

Tried and refused: taking the superuser rights from `reticula` in place, with `ALTER ROLE reticula NOSUPERUSER`, ends with `permission denied to alter role` and `The bootstrap superuser must have the SUPERUSER attribute`.

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

0.1.1 from PyPI started on a database that 0.2.0 had run on, and its check did not refuse: 0.2.0 adds no migration, so the ledger held none that 0.1.1 does not know. With nothing written in between, none of the 112 replies recorded under 0.1.1 before the switch differed afterwards. Notes written under 0.2.0 read back under 0.1.1, tokens minted and refreshed under 0.2.0 worked, and the smoke test of 0.1.1 passed. The two switches back left the server without an answer for 1.3 and 1.9 seconds.

## The key file

Each account has private keys that sign what it writes. The database holds them encrypted with a secret, the key file `deploy/secrets/reticula.key`. The key file is not in the database and not in its dumps. Someone who has a dump but not the key file cannot use the keys in it.

`compose.yaml` mounts the file into the server at `/run/secrets/reticula_key` and sets `RETICULA_KEY_FILE` to that path. Docker keeps the owner and mode the file has on the host. The server refuses to start when the file is missing, when it cannot read it, or when its mode lets the group or others read it. Measured, with the messages the log shows:

| The file | What the server did |
|---|---|
| mode 600, yours (Mac) or uid 10001 (Linux) | started |
| mode 644 | refused: `RETICULA_KEY_FILE '/run/secrets/reticula_key' has mode 0644: its group or others can use it`, and a hint with `chown` and `chmod 600` |
| missing | `docker compose up` ended with `bind source path does not exist` and the server did not start |
| another key file | refused: `24 stored private keys were encrypted with another secret` |

With Docker on a Mac a file of mode 400 is shown to the container as unreadable; use 600. Every `reticula-mcp` command needs the key file too. Run them with `docker compose exec app` or `docker compose run --rm --no-deps app`, which mount it.

Keep a copy of the key file apart from the plain database: never beside an unencrypted dump, which would undo what the encryption protects. A password manager, beside the private key of the backups, is one place. An encrypted copy written like `env.age` by `backup.sh` is another, because it opens only with the same private key as the encrypted dumps. Check the copy once with a restore on another machine.

Without the key file, the server does not start on that database, and neither does any `reticula-mcp` command except `key init`. They refuse with `N stored private keys were encrypted with another secret`. A new key file from `key init` does not help: the refusal stays. This release has no way back without the old file. Giving every account new keys is not built, and `RETICULA_KEY_FILE=none` starts the server but refuses the encrypted keys, so it cannot sign either. The key file encrypts only the private keys. Notes, hubs and accounts stay readable in the database and in every dump, and old signatures stay verifiable, because the public keys are stored apart and are not encrypted. A dump holds the keys encrypted with the key file that was in use when the dump was taken, so a restore needs that file. Measured: a dump taken before a rotation restored, and the server refused it with the new key file and started with the old one. Keep an old key file as long as you keep dumps taken with it.

To replace the key file, stop the server, make a new file and let `key rotate` re-encrypt every key with it. The new file is mounted for that one command:

```
docker compose stop app
(umask 077; openssl rand -base64 32 > secrets/reticula.key.new)
docker compose run --rm --no-deps -v ./secrets/reticula.key.new:/run/secrets/reticula_key_new:ro app reticula-mcp key rotate --new-file /run/secrets/reticula_key_new
mv secrets/reticula.key secrets/reticula.key.old
mv secrets/reticula.key.new secrets/reticula.key
docker compose up -d --wait
```

On Linux, `chown 10001:10001` the new file before the third line. The command ends with `now start the server with the new file as RETICULA_KEY_FILE`; with `compose.yaml` that is what the two `mv` lines do, which move it to `secrets/reticula.key`. Measured: `key rotate` took 1 second for 24 keys, and the server was back 8 seconds after it was stopped, with the tokens from before working.

## Back up

The whole instance is one PostgreSQL database. Dump it while the server runs. For a backup that is kept, use the encrypted command under [Encrypt the dump](#encrypt-the-dump). A plain dump is for a copy you use at once, for example before a restore:

```
(umask 077; docker compose exec -T db pg_dump -U reticula --format=custom reticula > reticula-$(date +%Y%m%d).dump)
```

`umask 077` makes the file readable by your own user only. The dump contains every note, the password hashes and the private keys of every account, unencrypted. The custom format is compressed, not encrypted: a search of the file for a line of a note finds nothing, and `pg_restore` turns the same file into readable SQL without asking for a key. Store the dump as carefully as the database itself. Keep a copy of `.env` with it: the dump does not contain the settings.

The embedding model is not in the dump. It is part of the image, which a build makes again.

A dump that stays on the machine is lost with the machine. A plain dump in another place is a second copy of every note and key, readable by whoever keeps it. The rest of this part encrypts the dump while it is written, takes it on a schedule and checks it. It was tested on release 0.1.1 with age 1.1.1 and GnuPG 2.2.40, the versions in Debian 12.

### Encrypt the dump

`age` and `gpg` both encrypt to a public key. The public key is on the machine that runs the database. The private key is not, so that machine writes backups and cannot read them, and neither can anyone who copies the files.

With `age`, make a key pair once, on your own computer:

```
age-keygen -o reticula-backup-key.txt
```

It prints `Public key: age1...`. The file holds the private key. Without it nobody reads the backups, you included. Keep a second copy in a place that does not depend on your computer, for example a password manager.

On the database machine, dump and encrypt in one command, with your public key after `-r`:

```
docker compose exec -T db pg_dump -U reticula --format=custom reticula | age -r age1... > reticula-$(date +%Y%m%d).dump.age
```

With `gpg`, export the public key of a key pair you have with `gpg --armor --export backup@example.com > reticula-backup-public.asc`, and import it on the database machine with `gpg --import < reticula-backup-public.asc`. Then:

```
docker compose exec -T db pg_dump -U reticula --format=custom reticula | gpg --batch --trust-model always --encrypt --recipient backup@example.com > reticula-$(date +%Y%m%d).dump.gpg
```

`--trust-model always` is needed because an imported key is not marked as trusted. Without it `gpg` ended with status 2 and `There is no assurance this key belongs to the named user`, and the file was empty.

Neither command writes a plain dump to disk. The encrypted files did not contain the text `PGDMP` that a plain dump starts with.

The shell reports the status of the last program of the line, and that is the encryption. With the database stopped, the `age` command ended with status 0 and left a file of 200 bytes, which is an encrypted dump of nothing. The script in the next part stops on a failed dump.

### On a schedule

The script also signs each file it writes, so that a copy can be checked later for a change on the way or in storage. The signature uses `ssh-keygen`, which comes with OpenSSH. Make the signing key once, on the database machine, in `deploy/`:

```
(umask 077; ssh-keygen -q -t ed25519 -N '' -C reticula-backup -f backup-signing.key)
printf 'reticula-backup %s\n' "$(cut -d' ' -f1,2 backup-signing.key.pub)" > allowed_signers
```

The first line writes the private key `backup-signing.key`, which stays on this machine and which git ignores, and its public half. The second writes `allowed_signers`, the file that checks a signature. Copy `allowed_signers` to your own computer, beside the private key of `age`. The signature tells you that a file comes from this machine unchanged. It cannot tell you more: whoever controls this machine can sign too.

Save this as `backup.sh` next to `compose.yaml` and `.env`, put your public key in the line `recipient=`, and run `chmod +x backup.sh`:

```
#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"

recipient=age1...
signing_key=backup-signing.key
dir=backups
keep_days=14

mkdir -p "$dir"
out="$dir/reticula-$(date +%Y%m%d-%H%M%S).dump.age"
trap 'rm -f "$out.part"' EXIT
docker compose exec -T db pg_dump -U reticula --format=custom reticula \
  | age -r "$recipient" > "$out.part"
mv "$out.part" "$out"
ssh-keygen -Y sign -q -f "$signing_key" -n reticula-backup "$out"
age -r "$recipient" < .env > "$dir/env.age"
rm -f "$dir/env.age.sig"
ssh-keygen -Y sign -q -f "$signing_key" -n reticula-backup "$dir/env.age"
find "$dir" -name 'reticula-*.dump.age*' -mtime +"$keep_days" -delete
```

Each run writes one encrypted dump into `backups/` with its signature in a file ending in `.sig`, replaces the encrypted copy of `.env` and its signature, and removes dumps and signatures older than 14 days. `ssh-keygen` does not overwrite a signature file: it stops and asks. That is why the old signature of `env.age` is removed first. Two runs of this script against a throwaway stack wrote three files and three signatures and nothing into the log, and every signature checked as under [Check a backup](#check-a-backup). A dump is written under a name that ends in `.part` and gets its own name when it is complete, so a file named `.dump.age` is a whole dump. `set -o pipefail` makes a failed dump fail the script. `umask 077` makes every file it writes readable by your own user only; that line was added after the tests below.

`backup.sh`, `backup.log`, `backups/`, `backup-signing.key` and `allowed_signers` are your own files in the clone. `git` does not track them, and moving the clone to another release, as in step 2 of "Upgrade", left them where they were.

For `gpg`, write `gpg --batch --trust-model always --encrypt --recipient "$recipient"` in place of `age -r "$recipient"` in both lines, set `recipient` to the address of the key, and let the names end in `.gpg`. Import the public key as the user whose crontab runs the script.

Run the script from cron. With `crontab -e`, for every night at 02:30:

```
30 2 * * * /home/reticula/reticula-mcp/deploy/backup.sh >> /home/reticula/reticula-mcp/deploy/backup.log 2>&1
```

In the test the cron of Debian 12 ran the `age` script from this line, with `* * * * *` as the time. It wrote the dump and the copy of `.env`, and nothing into the log. The `gpg` script was run without a terminal and with an empty environment, which is what cron gives a job, but not by cron itself. A systemd timer can start the same script. That was not tried.

What went wrong in the test, and what the script did then:

| Cause | Result |
|---|---|
| The database container is not running | Status 1, `service "db" is not running` in the log, no new file |
| `docker` is not where cron looks for programs | `docker: command not found` in the log, no new file |
| `age` is not installed | Status 127, `age: command not found` in the log, no new file |
| The public key is mistyped | Status 1, `age: error: malformed recipient` in the log, no new file |

The cron of Debian looks for programs in `/usr/bin` and `/bin` only. With `docker` in `/usr/local/bin` the job failed as in the second row. A line `PATH=/usr/local/bin:/usr/bin:/bin` at the top of the crontab made it work.

cron does not tell you about a failed run unless the machine can send mail. Look at `backup.log` and at the date of the newest file in `backups/`.

Do not paste the one-line command into a crontab as it is. In a crontab line `%` ends the command. The line with `$(date +%Y%m%d)` wrote no file at all, and the same line with every `%` written as `\%` wrote the dump. The script makes the date itself, so its crontab line has no `%`.

### Off the machine

Copy `backups/` to another machine after each run, with the tool you already use for copies. As a last line of the script, `rsync` does it:

```
rsync -a "$dir"/ backup.example.com:reticula-backups/
```

That line was not part of the test. The other machine gets encrypted files and no key, so it does not have to be trusted with the notes, only to keep the files. `rsync -a` removes nothing there, so the 14 days of the script apply to the database machine alone.

### Check a backup

Check the signature first, on your own computer, with `allowed_signers` from the database machine:

```
ssh-keygen -Y verify -f allowed_signers -I reticula-backup -n reticula-backup -s reticula-20261125-023000.dump.age.sig < reticula-20261125-023000.dump.age
```

Status 0 and `Good "reticula-backup" signature` say that the file is the one the database machine signed. Measured with OpenSSH 10.3 and with 9.2, the version of Debian 12:

| The file | Status and message |
|---|---|
| as written | 0, `Good "reticula-backup" signature for reticula-backup` |
| one byte changed | 255, `Signature verification failed: incorrect signature` |
| cut short | 255, `Signature verification failed: incorrect signature` |
| signed with another key | 255, `Could not verify signature.` |
| signed for another purpose (`-n`) | 255, `Couldn't verify signature: namespace does not match` |
| without its `.sig` file | 255, `Couldn't read signature file: No such file or directory` |

Do not decrypt or restore a file whose signature does not check. Then check the file itself. Decrypting it to nowhere reads all of it. That needs the private key, so do it on your own computer with a copy of the file:

```
age --decrypt -i reticula-backup-key.txt < reticula-20261125-023000.dump.age > /dev/null
```

Status 0 says that the file is whole and that this key opens it. A file cut off at 60 percent gave status 1 and `age: error: failed to decrypt and authenticate payload chunk`. Another key gave status 1 and `age: error: no identity matched any of the recipients`. `gpg --batch --decrypt` gave status 2 for a file cut off at the same place.

To see what a dump holds, send it through `pg_restore --list` on a machine that runs the stack. For 0.1.1 the list has 18 tables with data:

```
age --decrypt -i reticula-backup-key.txt < reticula-20261125-023000.dump.age | docker compose exec -T db pg_restore --list
```

The list stands at the start of a dump and does not show that the end is there: a plain dump cut off at 60 percent gave the same list with status 0. With `pg_restore -f /dev/null` in place of `pg_restore --list` the whole dump is read and nothing is written. That gave status 0 for a whole dump and status 1 for one that lacked its last 50 bytes. Use it for a plain dump, which has no check of its own.

The full check is a restore on another machine, with the steps of the next part.

## Restore

Restore into an empty database. The steps below replace the data of an existing stack. Everything written after the dump was taken is lost. On a new machine, start at step 2 with the same `.env`. The script under "Back up" keeps an encrypted copy of it: `age --decrypt -i reticula-backup-key.txt < env.age > .env` writes it back. A dump from a release with a key file also needs `secrets/reticula.key` as it was when the dump was taken; without it the server does not start on the restored data ([The key file](#the-key-file)).

The volume name in step 1 starts with the project name, which the compose file fixes to `reticula`. For a copy that was started under another project name, the volume is `<name>_reticula-db`, and every command below needs the same `-p <name>` or `COMPOSE_PROJECT_NAME`. Without it the commands act on the stack called `reticula`.

1. Check the signature of the dump as under [Check a backup](#check-a-backup), if it has one, and go on only with status 0. Stop the stack and remove the database volume. This deletes the current data. If any of it may still be needed, take a plain dump of it first, while the stack runs, as under Back up, and keep it until the restore is checked.

   ```
   (umask 077; docker compose exec -T db pg_dump -U reticula --format=custom reticula > reticula-before-restore.dump)
   docker compose down
   docker volume rm reticula_reticula-db
   ```

2. Start the database only. If the server starts first, it creates its tables and the restore fails on them.

   ```
   docker compose up -d --wait db
   ```

3. Restore the dump, as the role `reticula`. `--single-transaction` makes the restore all or nothing: it stops at the first error, and the database is then as empty as before. `--no-comments` leaves out the comments of the extensions, which only their owner `reticula_admin` may set; without it the restore stops at once with `must be owner of extension citext`.

   ```
   docker compose exec -T db pg_restore -U reticula --no-owner --no-comments --single-transaction --dbname reticula < reticula-20261125.dump
   ```

   An encrypted dump is decrypted into the same command. That needs the private key on this machine for the time of the restore. Remove it afterwards. With `age`, and with `gpg`:

   ```
   age --decrypt -i reticula-backup-key.txt < reticula-20261125-023000.dump.age | docker compose exec -T db pg_restore -U reticula --no-owner --no-comments --single-transaction --dbname reticula
   gpg --batch --decrypt < reticula-20261125-023000.dump.gpg | docker compose exec -T db pg_restore -U reticula --no-owner --no-comments --single-transaction --dbname reticula
   ```

   If the command ends with an error, nothing was restored. Do not go on to step 4: the server would start on the empty database as a new instance. Run step 3 again with a whole file.

4. Start the server. If the dump is older than the installed version, the server brings the schema up to date. If the dump is from a newer version, the server refuses to start, as described under "Go back to the older version". A dump from a release without a key file holds unencrypted keys, and the server refuses them with `a stored private key is not encrypted`; encrypt them first:

   ```
   docker compose run --rm --no-deps app reticula-mcp key encrypt
   docker compose up -d --build --wait
   ```

   The first line does nothing for a dump whose keys are encrypted already.

5. Check `/healthz` and run the smoke test.

In the drill the restored instance had the same number of rows in every table as the original. `docker compose exec app reticula-mcp rebuild` printed the same counts on both. Notes read back, shared and private. Accounts keep their person IDs, keys and tokens, so connected assistants keep working, as long as `RETICULA_PUBLIC_URL` is the same as before. With a different public URL, tokens issued at sign-in got status 401 and tokens minted with `reticula-mcp token` still worked.

The same steps took an instance back after an upgrade. A dump taken under 0.1.0 was restored after the upgrade to 0.1.1, with `RETICULA_VERSION=0.1.0` and the compose file of 0.1.0: steps 1 to 3 took 15 seconds and step 4 another 9. Every table had the row counts from before the upgrade, 112 recorded replies were the same as before it, and the notes written after the dump were gone. Started on that database again, 0.1.1 applied its two migrations as in the first upgrade.

The commands of step 3 were run on 0.2.0 with the plain dump, the `age` file and the `gpg` file. Each time every table had the row counts from the time of the backup, all 65 indexes and 53 constraints were there, and 112 recorded replies were the same as then. The smoke test passed on the restored instance. Step 3 took 0.2 seconds on a database of 232 rows, the same as without `--single-transaction`. On 0.1.1 the restore from the two encrypted files, then still without `--single-transaction`, gave the same result, and a note written after the backup was gone afterwards. There steps 1 to 3 took 7 seconds and step 4 another 7.

A damaged file ends step 3 with `pg_restore: error: could not read from input file: end of file`. That was tried with a dump cut off at 60 percent and with one that lacked its last 50 bytes, plain and encrypted with both tools. Each time the database held nothing afterwards: no table and no extension. Step 3 run again with the whole file then restored everything, without steps 1 and 2 in between.

Step 4 after a failed step 3 gives a new, empty instance and no error. The server answered `/healthz` with `ok`, logged 14 `applied` lines and had no account but its own, and a token from before the restore got status 401. The server cannot tell you that the restore failed. The status of step 3 does.

Without `--single-transaction` a damaged file leaves a part of the dump behind. Up to 0.2.0 this guide gave the command with `--exit-on-error` in its place. With that command the dump cut at 60 percent left 55 of 232 rows, and the server of step 4 did not start: its log said `0001_accounts_records.sql failed: relation "account" already exists`. The dump without its last 50 bytes left 226 of 232 rows, none of the 65 indexes, 5 of the 53 constraints and no trigger, and the server started on that and answered `ok`. A restore with the older command that ended without an error is whole.

## The activity log

Each account has a log of its own calls, which its owner reads on the account page. A line holds the time, the connection (the name the connector registered, or the label of a token from `reticula-mcp token`, with the short client id), the tool, whether it went through or the error code, and the short id of the note or hub the call named, but only when that note or hub is the account's own. Events on the account page itself, such as a sign-in there, an export or a revoked connection, are logged with `web` in place of the connection:

```
2026-11-26 12:03 UTC · Claude (c_4f2a9e1b) · put · ok · 542b1dbc
2026-11-26 12:09 UTC · unknown client · get · NotFound
2026-11-26 12:15 UTC · web · web sign-in · ok
```

The log never holds the text of a note, a query, a reply, or the id of another person's note, and nothing about who read the account's notes. The request log on stdout, under [Logs](#logs), is a separate thing and does not change.

It is kept for 30 days, set with `RETICULA_ACTIVITY_DAYS` in `.env`. `RETICULA_ACTIVITY_DAYS=0` turns it off: nothing is written. Days already logged then stay in the database until you drop them. The rows of one day form one table, `activity_YYYYMMDD`, and the server drops a day's table once it is older than the setting, at start and then hourly, and makes the tables of the next days ahead. When an account is deleted, its rows are deleted with it.

The server writes the lines about once a second, after the calls, in one statement. A line that cannot be written, for example while the database is away, is dropped. The call itself is not slowed down or refused for it, and the log says `activity` once a minute at most. A server that stops at once loses the last second of lines.

Measured on a test database:
- About 40 microseconds per call at the median, with the log on against off.
- One write of 200 lines took 5 to 7 ms, about 27,000 to 40,000 lines a second.
- 1,000 lines take about 134 KiB on disk, index included, so 30 days of 10,000 calls a day take about 40 MB.
- Deleting an account's 1,000 lines among 31,000 in 30 daily tables took 3 ms.

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

Claude Code is a client that takes such a header, and this is how it connects on a machine without a browser. `reticula` is the name the server gets in Claude Code, and the address is the one the client reaches the server under, with `/mcp` at the end. The label says where the token is used.

```
docker compose exec app reticula-mcp token --email someone@example.com --label "<where it is used>" --ttl-days 90
claude mcp add --transport http reticula https://<name>.<tailnet>.ts.net/mcp --header "Authorization: Bearer <token>"
claude mcp list
```

Measured on 2026-11-25, on a machine without a browser and through the public address of the Tailscale node of [Tailscale Funnel from a container](#tailscale-funnel-from-a-container): `claude mcp list` said `Connected`. The same command with `http://127.0.0.1:8787/mcp` was run on the machine of the server. Claude Code keeps the header, token included, in plain text in its configuration for the scope the server was added in. Its documentation, read on 2026-11-25: the default scope `local` and the scope `user` are kept in `~/.claude.json`, and `project` in `.mcp.json` in the project, which is meant to be committed. Do not add a server with a token with `--scope project`.

Claude Code 2.1.323 also has `claude mcp login --no-browser <name>`: it prints the sign-in address to open on another machine, and asks for the address the browser was sent back to. That was not tried.

List what an account has. Connected assistants come first, operator tokens minted with `reticula-mcp token` in a second block headed `operator tokens`, with the expiry of each. Each row has an id.

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

The image contains the `fastembed` extra, and with `RETICULA_EMBEDDER=auto` the server uses it. The model, `BAAI/bge-base-en-v1.5` since 0.3.0, is built into the image from the next release after 0.4.1 on. The build fetches its files from Hugging Face at a fixed revision and checks the hash of each one (`deploy/fetch-model.py`). The running server downloads nothing (`HF_HUB_OFFLINE=1`), so only the build needs outbound HTTPS. 0.2.0 and earlier used `BAAI/bge-small-en-v1.5`; see [Choose a model](#choose-a-model).

The model makes the image larger. Measured: 988 MB with bge-base, against 570 MB without a model. Up to 0.4.1 the server downloaded the model at its first start and kept it in the volume `reticula-models`; that volume is no longer used, and `docker volume rm reticula_reticula-models` removes it.

The server loads the model in the background when it starts, and `/healthz` does not wait for it. With `BAAI/bge-small-en-v1.5`, `/healthz` answered after 1 second and the log line `embedding model BAAI/bge-small-en-v1.5 loaded in 2.7s` followed, download included; with the model in place the line says 0.4 seconds.

If the model cannot be loaded, the log has errors from `fastembed` and the warning `embedding model ... did not load; search answers from keywords and the load is retried later`. The server keeps running.

With `BAAI/bge-small-en-v1.5` the server container used 190 MiB of memory with the model loaded, and the start that downloads the model peaked at 268 MiB, the file cache of the download included. `BAAI/bge-base-en-v1.5` needs about 500 MB more (see [Choose a model](#choose-a-model)).

`RETICULA_EMBEDDER=hash` needs no download. Search then matches keywords only.

### The vector index

A search compares the query with every vector the account may read, as long as those are at most 20,000. Above that it uses a vector index in the database. Each model has its own index. The index needs pgvector 0.8 or newer; the database image of `compose.yaml` has 0.8.5.

- The server creates the index of its model at start, once the model is loaded. At the first start the log has the line `vector index embedding_hnsw_f1f52874959c_384 ready for fastembed:BAAI/bge-small-en-v1.5`. With `RETICULA_EMBEDDER=hash` the server does not create one.
- A search never creates the index. After a switch to another model, a search compares every vector until the index of the new model exists. Measured after a switch to `hash`: a start and a search left that model without an index, and `docker compose exec app reticula-mcp reindex` created it. Without the index a search still answers correctly. It only gets slower, and only above 20,000 vectors.
- A search chooses between the two ways by the row estimate PostgreSQL keeps for the table. Right after many vectors were written at once the estimate is still the old one, and a search keeps comparing every vector although the index exists. PostgreSQL corrects the estimate by itself: with 30,000 rows added at once it took 41 seconds. To correct it at once, which took 1 second:

  ```
  docker compose exec -T db psql -U reticula -d reticula -c 'ANALYZE embedding'
  ```

### Choose a model

`RETICULA_EMBED_MODEL` names the model. The server knows three: each brings its own cut, the similarity below which a match by meaning is not shown, and bge-base its own instruction in front of every query. Any other model of `fastembed` can be named too; its cut is then bge-small's, and the log says that it has not been measured.

| Model | Vectors | Download | Memory with the model loaded | Embedding 1,000 notes | Added to a put of a long note | Paraphrases found | Nonsense answered among 6,000 notes |
|---|---|---|---|---|---|---|---|
| `BAAI/bge-small-en-v1.5` | 384 | 67 MB | 0.3 GB | about 40 s | 0.2 s | 15 of 23 | 3 of 6 |
| `BAAI/bge-base-en-v1.5` (default) | 768 | 219 MB | 0.8 GB | about 2 min | 0.6 s | 17 of 23 | none |
| `mixedbread-ai/mxbai-embed-large-v1` | 1024 | 1.34 GB | 1.8 GB | about 6 min | 2 s | 18 of 23 | none |

Measured on a development machine with 2 threads, as the server runs the model, while other work ran on it: take the times as an order of size. Memory is that of a process with only the model loaded. A put embeds its note after it is saved and before it answers, so the time in the sixth column is added to the answer of `put` and `edit` for a note of 4,000 characters or more; nothing is locked meanwhile. The last two columns are from the relevance set of the tests, with 6,000 notes of the same subjects added, at each model's own cut: the paraphrases whose note is shown, and the nonsense queries that get a note anyway. mxbai finds more of the hard cases (4 of 5 where bge-base finds 2), at three times the cost.

The server container has a memory limit of 2 GB from 0.4.1 on, set with `RETICULA_APP_MEMORY` in `.env`. That fits bge-small and bge-base, also during a `reindex` in the container, which loads the model a second time. For mxbai set at least twice its memory from the table, for example `RETICULA_APP_MEMORY=4g`; that value was not measured.

Every model reads the first 512 tokens of a note, about 2,300 characters of English. The rest of a longer note is found by its words but not by its meaning.

### Change the model

`RETICULA_EMBED_MODEL` in `.env` names another model. The image is built with it, so apply it with `docker compose up -d --build --wait`. The image can carry only the models whose files `deploy/fetch-model.py` lists with their hashes: `BAAI/bge-small-en-v1.5`, `BAAI/bge-base-en-v1.5`, `mixedbread-ai/mxbai-embed-large-v1` and `sentence-transformers/all-MiniLM-L6-v2`. Any other name ends the build with an error that names those four. Measured, each embedding offline in the built image: bge-small and MiniLM gave 384 numbers and an image of 699 and 745 MB, bge-base 768 and 988 MB, mxbai 1024 and 2.71 GB. The build of the mxbai image took 59 seconds with its 1.34 GB of files.

The following was measured up to 0.4.1, when the server downloaded a model at its start, with `sentence-transformers/all-MiniLM-L6-v2` and with `jinaai/jina-embeddings-v2-small-en`, which has another vector size:

- The server downloaded the model and was healthy after 7 seconds. The log names the model: `embedding model sentence-transformers/all-MiniLM-L6-v2 loaded in 2.6s`, then `vector index embedding_hnsw_40541eb5df3b_384 ready for fastembed:sentence-transformers/all-MiniLM-L6-v2`.
- The server then computes the vectors of all notes under the new model by itself, in the background. All 6 notes of the test instance had theirs 7 seconds after the switch, with no command run. Search works in the meantime.
- `docker compose exec app reticula-mcp reindex` does the same work in one run and returns when it is done. Run it on a large instance to know when the new model is complete. After a switch to `RETICULA_EMBEDDER=hash` it is needed: the server does not create that index by itself.
- The vectors and the index of the old model stay in the database, and its files stay in the volume. A switch back before they are removed needs no reindex: they were used again at once.
- When the new model has a vector for every note, `docker compose exec app reticula-mcp reindex --drop-other-models` removes the vectors and the vector indexes of every other model, and embeds nothing. Before that it refuses, removes nothing and says how many notes are still waiting. It does not remove model files from the volume.
- `docker compose exec app reticula-mcp reindex --drop` empties the index of every model and embeds every note again under the current one.
- A name `fastembed` does not know: the server starts and stays healthy, and the log has `embedding model fastembed:no/such-model did not load; search answers from keywords; next try in 30s` with `ValueError: Model no/such-model is not supported in TextEmbedding`. Saving and searching still worked. With the model in the image such a name now ends the build instead.

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
| Embedding model | in the image, under `/opt/reticula/models`; up to 0.4.1 the volume `reticula_reticula-models` |
| Settings and the two database passwords | `deploy/.env`, which git ignores. The server container gets only the password of `reticula` |
| Backups written by `backup.sh` | `deploy/backups/`, encrypted and signed. The key that opens them is not on this machine |
| The key that signs the backups | `deploy/backup-signing.key`; the file that checks the signatures, `allowed_signers`, also on your own computer |
| The key file that encrypts the accounts' private keys | `deploy/secrets/reticula.key`, which git ignores. Its copy belongs elsewhere, not beside the backups |
| The identity of the Tailscale node of `compose.tailscale.yaml` | Docker volume `reticula_reticula-tailscale` |
| Shipped instruction texts | inside the installed package; copied into the database when migrations run |

## Without Docker

The server is one command and needs a PostgreSQL database with the pgvector extension. Set `RETICULA_DATABASE_URL` and the other settings in the environment. With `uvx`, write `uvx reticula-mcp ...` where this page writes `reticula-mcp ...`.

```
reticula-mcp migrate
reticula-mcp serve
```

`serve` refuses to start when migrations are pending. `serve --migrate` applies them first. `serve --host H --port P` overrides `RETICULA_HOST` and `RETICULA_PORT`. The defaults are `127.0.0.1` and `8787`. Run the server under a process manager so it comes back after a reboot.

`migrate` is safe to run any number of times. Besides applying schema files, it creates the system account (handle `reticula`, no password, cannot sign in) and loads the shipped instructions. The instructions are written as notes by the system account, in a hub that every account can read. It also creates the debug hub. A second run changes nothing. A new version of a shipped note is written only when its text changed in the release you installed.

The other commands on this page are the same without the `docker compose exec app` prefix. Back up with `pg_dump --format=custom "$RETICULA_DATABASE_URL"`, and restore with `pg_restore --no-owner --no-comments --single-transaction` into an empty database before the first `migrate`; the forms of these commands without Docker were not run.

A tunnel or proxy on the same host reaches a server that runs as a host process from `127.0.0.1`. That is then the value for `RETICULA_TRUSTED_PROXIES`.
