# Serve and connect

A workflow that matters to a team runs on a machine the team shares: a build host, a bastion, a
container next to the data. `winslow serve` runs the engine there, and everyone works against it
from their own terminal with `winslow connect`. The sessions live on the host, so a batch keeps
running when a laptop closes, two people watch the same run, and the History tab is the record of
what the team did. With `--endpoints ws web mcp` the same host also serves agents over MCP, with the
same reads and actions a terminal has.

Compared to `winslow run`, nothing changes in the workflow code or in the UI. The engine moves out
of the terminal into its own process, and the terminal becomes one client of it.

The page starts with the two setups most people want, [a team on one host](#a-team-shares-one-host)
and [an agent over MCP](#connect-an-agent). The rest is the reference: the
[process picture](#the-process-picture), the [extras](#install), the [serve](#start-a-server) and
[connect](#connect-a-terminal) commands, the [web UI](#open-the-web-ui), the
[credential rules](#credentials), the [MCP endpoint](#the-mcp-endpoint) and the [log stream](#logs).

## A team shares one host

The engine is the place where the tasks run. Everything else is a client of it. The host runs the
server behind the proxy that ends TLS at `wss://etl-host` and mints one token per engineer:

```bash
winslow serve --host 0.0.0.0
winslow auth token create can          # prints wsl_... once
```

Each engineer stores their token once and connects with it:

```bash
uvx --from "winslow[connect]" winslow auth login wss://etl-host   # prompts, no echo
uvx --from "winslow[connect]" winslow connect wss://etl-host
```

A `ws://` URL to the host sends the token in clear text, so `login` stores one for it with
`--insecure` alone (see [Log in once](#log-in-once)).

Both see the same sessions. A batch one engineer starts shows its task states and its log lines on the
other screen as they happen, and the History tab of both lists it when it completes. Each history row
names the user that submitted the batch, each session row the user that started the session, and a
cache entry the user whose load wrote it. The name is the one the server accepted at the connection: the
user of a token or a ticket, `local` on a loopback bind. A history row of a token or a ticket also names
the id of the credential beside its kind, `can (token 9f86d081)`, so two tokens of one service account
are told apart.

## Connect an agent

An agent gets the same reads and actions as a terminal, over MCP. On the host:

1. Install `winslow[mcp]` on the host (see [Install](#install)).

2. Serve both endpoints:

    ```bash
    winslow serve --endpoints ws web mcp
    ```

    The MCP endpoint is at `http://127.0.0.1:8866/mcp`. On a shared host the server binds the network
    and the agent sends its token as a bearer header, from a variable of its own environment (see
    [Credentials](#credentials)).

3. Register the server in the agent and check the connection:

    === "Claude"

        ```bash
        claude mcp add --transport http winslow http://127.0.0.1:8866/mcp
        ```

        On a shared host:

        ```bash
        claude mcp add --transport http winslow http://etl-host:8866/mcp \
          --header "Authorization: Bearer a-long-random-string"
        ```

        `claude mcp list` shows `winslow` as connected, and `/mcp` inside a session lists its tools.

        Claude Desktop and claude.ai connect to a custom connector from Anthropic's cloud, so they
        reach a server on the public internet over HTTPS only. Add its URL under Customize > Connectors.

    === "Codex CLI"

        ```bash
        codex mcp add winslow --url http://127.0.0.1:8866/mcp
        ```

        On a shared host, Codex reads the token from the environment:

        ```bash
        codex mcp add winslow --url http://etl-host:8866/mcp --bearer-token-env-var WINSLOW_TOKEN
        ```

        Both write a `[mcp_servers.winslow]` table to `~/.codex/config.toml`. `codex mcp list` shows
        `winslow`.

    === "Gemini CLI"

        ```bash
        gemini mcp add --transport http winslow http://127.0.0.1:8866/mcp
        ```

        On a shared host:

        ```bash
        gemini mcp add --transport http winslow http://etl-host:8866/mcp \
          --header "Authorization: Bearer a-long-random-string"
        ```

        Both write the server to `~/.gemini/settings.json`. `/mcp` inside a session lists `winslow` and
        its tools.

    === "Cursor"

        ```json title=".cursor/mcp.json"
        {"mcpServers": {"winslow": {"url": "http://127.0.0.1:8866/mcp"}}}
        ```

        On a shared host, Cursor reads the token from the environment:

        ```json title=".cursor/mcp.json"
        {
          "mcpServers": {
            "winslow": {
              "url": "http://etl-host:8866/mcp",
              "headers": {"Authorization": "Bearer ${env:WINSLOW_TOKEN}"}
            }
          }
        }
        ```

        Cursor has no add command: the file is the configuration. The MCP page of the Cursor settings
        shows `winslow` with its tools.

    === "VS Code"

        ```bash
        code --add-mcp '{"name": "winslow", "type": "http", "url": "http://127.0.0.1:8866/mcp"}'
        ```

        On a shared host:

        ```bash
        code --add-mcp '{"name": "winslow", "type": "http", "url": "http://etl-host:8866/mcp",
          "headers": {"Authorization": "Bearer wsl_9f86d081884c7d659a2feaa0c55ad015"}}'
        ```

        The same object, without `name`, goes under `servers` in `.vscode/mcp.json` for a checked-in
        project config. The MCP Servers view of the Extensions panel shows `winslow` as running.

4. Ask. "Which sessions are live?" calls `list_sessions`. "Start etl" calls `descriptors` for the options,
   then `start_session`. "What failed in last night's run?" calls `history` for the batches with the outcome
   of each task, then `task_detail` for the traceback. "Run it again" calls `run_tasks` with the key of the
   task and `{"force_run": true}`. Every action answers an ack, accepted with the batch uuid or refused with
   the reason, and the connected terminals show the batch the agent started.

## The process picture

The engine owns everything durable: the workflow code, the session registry, the state store and
the log files. Tasks run there. A client holds no session of its own. It reads snapshots and
submits actions over the socket, and the server applies them to the engine.

Each connection subscribes to the sessions it shows. One bridge per session fans the session
events out to every subscribed connection, and one sender task per socket writes the frames in
order. Task log lines coalesce per task every 50 ms, so a task that logs many lines costs one
frame per tick.

## Install

| extra | installs | for |
|---|---|---|
| `winslow[serve]` | starlette, uvicorn, websockets, pydantic, psutil, the built web UI | the server |
| `winslow[connect]` | websockets, pydantic and the TUI packages | a remote terminal, through `uvx` or a project |
| `winslow[mcp]` | the serve packages plus mcp | a server with the MCP endpoint |

=== "uv"

    ```bash
    uv add "winslow[serve]"
    ```

=== "pip"

    ```bash
    pip install 'winslow[serve]'
    ```

## Start a server

```bash
winslow serve                              # 127.0.0.1:8866, websocket at /ws, web UI at /
winslow serve --host 0.0.0.0 --port 9000   # reachable from other hosts
winslow serve --endpoints ws web mcp       # adds the MCP endpoint at /mcp
winslow serve --endpoints ws               # the websocket alone, no page at /
winslow serve --endpoints mcp              # MCP only
```

The endpoints are `ws`, the websocket the terminal and the web UI talk to, `web`, the browser UI
at `/`, and `mcp`. The default is `ws web`.

The engine reads the workflows of the working directory, like `winslow run`. At startup it
rebuilds every open manifest of the state directory, so the sessions of a dead process come back
before the first client connects (see [Sessions and restore](sessions.md#restore-in-serve-mode)).
A workflow with `auto_init = True` starts one session in the engine at the same point.

### Stop a server

Ctrl+C stops the server. It ends the MCP listen streams with their final frame, closes the open
connections, then asks every running batch to stop and waits for the workers. Each wait has a bound
of five seconds. A batch stops at its next task boundary, so a task that is mid run completes first.

## Connect a terminal

A terminal needs no project. `uvx` fetches the client and runs it:

```bash
uvx --from "winslow[connect]" winslow connect ws://127.0.0.1:8866
uvx --from "winslow[connect]" winslow auth login wss://build-host    # once, prompts for the token
uvx --from "winslow[connect]" winslow connect wss://build-host
```

A machine that connects often adds `winslow[connect]` to a project and runs `winslow connect` from
there. The URL is the one positional argument. `connect` sends the credential `winslow auth login`
stored for the URL (see [Log in once](#log-in-once)). The dashboard and the workflow screens are the
ones of `winslow run`, drawn from the snapshots the server sends. Every action goes to the engine,
and the task logs stream back over the socket.

## Open the web UI

The serve process hands out a browser UI at the root of its address:

```bash
winslow serve
open http://127.0.0.1:8866/
```

The page shows the dashboard and the session screens of the terminal, drawn from the same snapshots
and events: the start form, the session rows, the task list with run and check, the History and
Caches tabs, the task and cache detail views, and the session log. It connects to the websocket of
its own origin, and the server lists that origin itself, so a loopback bind needs no configuration.

A front application that embeds the page sets `window.WINSLOW` before the app script loads:

```html
<script>
  window.WINSLOW = {
    wsUrl: "wss://etl-host:8866/ws",
    credential: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW4iLC...",
    credentialUrl: "/winslow/ticket/",
  };
</script>
```

`wsUrl` names the websocket, `credential` is the ticket of the page (see
[Tickets for browsers](#tickets-for-browsers)), and `credentialUrl` answers a fresh one as text: the
page fetches it when the ticket expires and reconnects. The origin of such a page goes in
`SERVE_ORIGINS`. The source of the page is `web/` in the repository (see `web/README.md`).

## Credentials

A shared host names each person and each service account with a credential of its own, and a policy
file says what each one may do. The server reads these from its [settings module](settings.md):

| setting | holds |
|---|---|
| `AUTH_BACKENDS` | the credential kinds the server accepts, in order: tokens, tickets for a browser page, OIDC tokens |
| `AUTH_POLICY_FILE` | the policy: groups, users, scopes. Default `.winslow/policy.toml` |
| `AUTH_TOKENS_FILE` | the per-user tokens, as hashes. Default `.winslow/tokens` |
| `AUTH_TICKET_SECRET` | the secret that signs the tickets, 32 bytes or more (`openssl rand -base64 32`) |
| `SERVE_PUBLIC_URL` | the URL clients reach the server on, the audience of every ticket. Required with the secret |
| `SERVE_ORIGINS` | the browser origins the websocket endpoint accepts, a tuple |
| `AUTH_OPEN` | `True` passes every connection with every permission, for development |
| `CREDENTIALS_FILE` | on the client: the credential file of the person. Default `~/.config/winslow/credentials.toml` |

Both file paths are relative to the working directory. Run `winslow auth` in the directory where
`winslow serve` runs, so both read one file.

### The policy file

```toml
[groups.etl-operators]
scopes = ["etl:*"]

[groups.etl-checkers]
scopes = ["etl:check_task", "etl:start_workflow"]

[groups.viewers]
scopes = ["*:view_task"]

[users.can]
groups = ["etl-operators", "viewers"]

[users.ci-bot]
scopes = ["etl:run_task"]
```

A scope is `<scope name>:<permission>`: the scope name of a workflow (see
[workflows](workflows.md#the-scope-name-of-a-workflow)) or `*` for every workflow, and one of
`start_workflow`, `end_workflow`, `run_task`, `check_task`, `view_task`, or `*` for all five.
`run_task` includes `check_task`, and every permission includes `view_task`. A user holds the scopes of
their groups plus their direct scopes. A group table holds `scopes` and `idp_groups`, a user table
`groups` and `scopes`. A group or user name is one word of letters, digits and `. _ @ -`. The server
reads the file again when it changes, so an edit applies to the next connection. An edit the schema
refuses keeps the previous policy and logs the field and the reason. At startup the server logs a
WARNING for every group or user that grants every permission on every workflow, and for every group
without scopes.

### Per-user tokens

```bash
winslow auth token create can                   # prints the token once, stores its hash
winslow auth token create nobody                # notes on stderr that the user holds no scope
winslow auth token create ci-bot --expires 90d
winslow auth token list can
winslow auth token revoke --token-id 9f86d081   # one token by id
winslow auth token revoke --user can            # every token of the user
winslow auth show can                           # scopes, token count
winslow auth check can etl run_task
```

A token starts with `wsl_`, so a secret scanner catches one that lands in a commit. The token file
holds SHA-256 hashes alone. A token travels in the connection request of `winslow connect`, from the credential file,
in the `Authorization: Bearer` header of an MCP client, and in the token field the web page shows when
the server refuses a connection without one. It grants what its user holds in the policy at the time
of the connection.

`winslow serve` logs at startup which stores it uses and which credentials it accepts.

### Log in once

```bash
winslow auth login wss://etl-host                   # prompts for the token, no echo
echo "$TOKEN" | winslow auth login wss://etl-host --with-token   # a script reads it from stdin
winslow auth login wss://etl-host --kind ticket     # a credential of another listed kind
winslow connect wss://etl-host                      # sends the stored credential
winslow auth status                                 # wss://etl-host  can  token
winslow auth logout wss://etl-host
```

The credential lives in `~/.config/winslow/credentials.toml` (`CREDENTIALS_FILE`), mode 0600, one
table per server, with the kind of the credential beside it. `connect` writes the user the server
named after the first connection, so `status` names the person. A `ws://` URL to a host other than loopback
sends the token in clear text, so `login` stores one for it with `--insecure` alone. `--kind` names
the backend that obtains the credential, one of `AUTH_BACKENDS` of the client, the first listed by
default. A job stores its token with `--with-token` before it connects.

The rules:

- A loopback bind in the `local` env needs no credential. `127.0.0.1`, `::1` and `localhost` accept
  every connection while `ENV` holds its default. Every process on the machine reaches a loopback server.
  A deployment sets `ENV`, so a loopback bind behind its proxy requires a credential like any other.
- Every other bind needs a credential on every connection: a token or a ticket, in the `token` field
  of the connection request.
  The MCP endpoint on such a bind accepts the same credentials as bearer headers.
- `AUTH_OPEN = True` is for a shared development box: every connection passes on any bind with every
  permission, a known token on the websocket still names its user in the history, and the server
  logs one WARNING at startup. The MCP endpoint in open mode records every call as `anonymous`. The
  Origin rule stays on.
- A browser origin must be listed. A connection request that carries an `Origin` header passes when the
  header value is in `SERVE_ORIGINS`. This holds on every bind, the loopback one included, so a
  web page in the operator's browser reaches a local server only when its origin is listed. A
  client without a browser sends no `Origin` header and is unaffected.
- A credential lives one lifetime on a socket. The server closes a connection when its credential
  expires, with close code 4402, and the client reconnects at once with a fresh one: the page fetches a
  new ticket, `winslow connect` renews the stored credential through its backend, the way `auth login`
  obtained it. Both show a "credential renewed" notice. A
  credential holds a socket for `AUTH_MAX_CONNECTION`, one hour by default. A ticket, or a token
  created with `--expires`, holds it for the shorter of its own expiry and that bound. A change of what a
  credential may do thus reaches a live connection within one lifetime.
- A credential with `*:*` grants every action on the host. A client with one starts sessions, which
  imports and runs the project code, runs and stops tasks, ends sessions and clears caches. Task
  details carry tracebacks and absolute paths of the host. Grant it to the people you would give a
  shell on that host.

An origin is scheme, host and port, with no path and no trailing slash. `http://localhost:5173`
and `http://127.0.0.1:5173` are two origins.

The engine holds every session and the server every connection a client asks for. Size the host
for the clients you expect.

### Tickets for browsers

A browser client authenticates against your front application, which decides what the user may do
and mints a ticket that says so. `winslow.serve.mint_ticket` and the server share one rule:

```python
from winslow.serve import mint_ticket

ticket = mint_ticket(
    secret,
    user="can",
    scopes=["etl:run_task", "reports:view_task"],
    audience="https://etl-host",   # the SERVE_PUBLIC_URL of the server
    ttl=900,                       # the default, one hour at most
)
```

A ticket is a JWT signed with HS256 under `AUTH_TICKET_SECRET`. Its claims are `sub`, the user,
`scope`, the scopes it grants, `aud`, the server it is for, `exp` and `iat`, and `jti`, the id of
this ticket, plus `iss` and `nbf` when the issuer sets them. `mint_ticket` applies the same rules as
the server, so a wrong claim fails at the line that mints it. The page and a script send it as the
token of their connection request, an MCP client in the bearer header. The server checks the signature, the
audience and the expiry, tolerates a minting clock up to a minute ahead, and builds the permissions
of the connection from the `scope` claim. The ticket alone decides the permissions of the
connection. A ticket lives an hour at most: the page fetches a fresh one at the expiry, so `ttl` is
how long a logout in the front application takes to reach an open page.

### An OpenID Connect issuer

A deployment with an identity provider lists the OpenID Connect backend and names the issuer. The
server then accepts the access tokens of that issuer, from an agent as a bearer header and from a
terminal through `winslow auth login --kind oidc`, which runs the login in the browser:

```python title="etl/settings.py"
AUTH_BACKENDS = [
    "winslow.contrib.oidc.OidcBackend",
    "winslow.access.backends.TokenBackend",
]
SERVE_PUBLIC_URL = "https://etl-host"
OIDC_ISSUER = "https://example.okta.com/oauth2/default"
OIDC_CLIENT_IDS = ("0oa...",)          # the clients the issuer registered for this server
OIDC_ALLOWED_DOMAINS = ("example.com",)
OIDC_CLIENT_ID = "0oa..."              # the client `auth login` logs in with
```

The server verifies the signature against the key set the discovery document of the issuer names,
the issuer, the audience, which is `SERVE_PUBLIC_URL`, and the expiry, with a minute of leeway on the
clocks. A key id outside the cached key set is
refused until the set expires, so a bad token costs no fetch. With the allow-list set, the token must
name one of the clients in its `cid`, `client_id` or `azp` claim, and an audience shared with other
apps of the issuer needs the allow-list. The user is the `sub` claim, at one of the allowed domains
when any are set. The groups of the `groups` claim reach the
policy as the groups the token asserts, so a person the policy file does not list holds the scopes of
the groups whose `idp_groups` match. List the backend before the ticket backend: both accept a JWT,
and the OpenID backend accepts the ones of its issuer alone.

The login asks the issuer for a refresh token with the `offline_access` scope and stores it beside
the access token. When the stored token has expired, `winslow connect` exchanges the refresh token
for a fresh one before it connects, so one login lasts as long as the issuer allows.

An issuer with other conventions is a subclass with class attributes. Google, for example, hands out
an opaque access token and puts the identity in the id token, names the user in `email`, asks for
its refresh token with a query parameter, and demands the client secret of an installed app:

```python title="etl/auth.py"
from winslow.contrib.oidc import OidcBackend


class GoogleBackend(OidcBackend):
    name = "google"
    token_field = "id_token"
    user_claim = "email"
    scopes = ("openid", "email")
    login_query = {"access_type": "offline", "prompt": "consent"}
```

With `OIDC_ISSUER = "https://accounts.google.com"`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` from the
Google Cloud console, and the client id as the audience and the allow-list, since Google stamps the
client id as `aud`.

### Another permission rule

A backend answers two questions: `identify` says who presented the credential, `scopes_for` says what
they may do. The policy file is the default answer of the second. A deployment with its own rule, the
groups of the issuer as scopes or the tables of another application, subclasses one backend and
overrides the one method, then lists the subclass:

```python title="etl/auth.py"
from winslow.access.permissions import expand
from winslow.contrib.oidc import OidcBackend


class OktaBackend(OidcBackend):
    def scopes_for(self, identity):
        # A group named etl-run grants etl:run_task.
        return expand(f"{g[:-4]}:run_task" for g in identity.groups if g.endswith("-run"))
```

```python title="etl/settings.py"
AUTH_BACKENDS = ["etl.auth.OktaBackend", "winslow.access.backends.TokenBackend"]
```

The identity carries the user, the kind, the id and the expiry of the credential, and the groups it
asserts. One backend answers per credential, the first whose shape matches the token.

## The environment color

The header of every client names the service and the environment of the server, `etl` and `prod`,
and takes the color of the environment: the TUI header over `winslow connect`, the navbar of the web
UI.
The server reads the two names from the `SERVICE_NAME` and `ENV` settings and the color from the
`ENV_COLORS` table, keyed by the environment:

| environment | color |
|---|---|
| `prod` | red |
| `uat` | gold |
| `staging` | orange |
| `dev` | green |
| `local` | royalblue |

A settings module replaces the table with its own. A value is a CSS color name or a hex value:

```python title="etl/settings.py"
ENV = "qa"
ENV_COLORS = {"qa": "purple", "prod": "red", "local": "royalblue"}
```

A name outside the table shows in gray.

## The websocket handshake

The first message on the socket is a hello with the credential of the client, a token or a ticket:

```json
{"type": "hello", "token": "wsl_..."}
```

The server answers `hello_ok` with the user it resolved, the permissions of the connection, the kind
of credential, its id and its expiry:

```json
{
  "type": "hello_ok",
  "user": "can",
  "scopes": ["etl:check_task", "etl:run_task", "etl:view_task"],
  "credential": "token",
  "credential_id": "9f86d081",
  "expires_at": 1757400000.0
}
```

`scopes` lists `<scope name>:<permission>` strings, already expanded: `run_task` brings `check_task`
and `view_task` with it, and `*` as the scope name covers every workflow. A client enables a control
when the list holds its permission on the session's scope name, and renders it disabled before the
hello and after a drop. `credential` names its kind: `local`, `token`, `ticket`, and `credential_id`
the id the records carry, the token id or the ticket id (see
[A team shares one host](#a-team-shares-one-host)). `expires_at` is the expiry of the credential the
server resolved, and the second the server closes the socket with code 4402 (see
[Credentials](#credentials)).

A refused hello receives a `hello_error` frame with the reason, then the socket closes with a
code that names the refusal class:

| code | refusal |
|---|---|
| 4400 | the first message is not a hello |
| 4401 | the credential is refused |
| 4403 | the origin of the page is refused |
| 4408 | no connection request within the timeout |

After `hello_ok` the client subscribes to sessions and submits actions over the same socket.
`winslow.protocol.frames` declares every frame, and `winslow.client.websocket` is the reference
client.

## The MCP endpoint

`--endpoints ws web mcp`, or `mcp` alone, mounts an MCP server at `/mcp` over streamable HTTP. The
server is named `<SERVICE_NAME>-<ENV>`, `etl-prod`, and its instructions open with the two names.
Register it in a client under that same name, `claude mcp add --transport http etl-prod ...`, since
the client's name is the tool prefix an agent sees, `mcp__etl-prod__run_tasks`, and an agent with
several deployments tells them apart by it. It is the tool layer over the same port: the
tools read snapshots and submit actions, and every action tool answers the ack as data, accepted
with the batch uuid or refused with the reason, so an agent reads the outcome as a value.

An agent sends its credential as a bearer header, the example below shows the shape. With an
OIDC backend listed, the protected resource metadata of the endpoint names the issuer as
the authorization server, so an agent that speaks OAuth, Claude Code among them, logs in there by
itself: it registers with the issuer, opens the browser once, and sends the token it is given.

| group | tools |
|---|---|
| discover | `list_sessions`, `descriptors`, `manifests`, `host_stats` |
| sessions | `start_session`, `restore_session`, `end_session` |
| read | `snapshot`, `roster`, `task_detail`, `history`, `record_detail` |
| act | `run_tasks`, `check_tasks`, `stop_batch`, `load_cache_entries`, `clear_cache_entries` |

`descriptors` lists the workflows the engine can start with their options. Its rows fill the
`values` of `start_session`. `host_stats` reports the memory, the cpu load, the hostname and the user
of the machine the sessions run on. `run_tasks` and `check_tasks` take task identity keys from
`snapshot` or `roster`, plus the batch flags as `options`, for example `{"force_run": true}`.

An MCP client on a non-loopback bind sends the token as a bearer:

```json
{
  "mcpServers": {
    "winslow": {
      "url": "http://build-host:8866/mcp",
      "headers": {"Authorization": "Bearer wsl_9f86d081884c7d659a2feaa0c55ad015"}
    }
  }
}
```

## Logs

The default keeps the log files of `winslow run`: one file per session under `LOG_DIR`.
`LOG_JSON = True` in the settings module sends one JSON object per record to stdout instead, task
logs included, each stamped with the session and task fields, so a pod writes one stream for its log
store:

```python title="etl/settings.py"
LOG_JSON = True
```

```json
{"ts": "2026-09-08 10:15:02 UTC", "level": "INFO", "session_id": "etl-20260908-101459",
 "workflow_name": "etl", "workflow_instance": "etl", "task_name": "DownloadData",
 "task_instance": "DownloadData", "batch_uuid": "5f1c...", "user": "can", "credential": "ticket",
 "credential_id": "4c1f...", "message": "downloaded 3 files", "traceback": null}
```

`user`, `credential` and `credential_id` name the connection whose request caused the line: the user
the server resolved, the kind of credential it presented and the id of that credential, the token id or
the ticket id. A batch a client submits carries them on every line of its run. A line of the local TUI
or a headless run carries `null`.

The uvicorn lines share the same handler, so the whole process writes one format.

### The audit lines

The `winslow.audit` logger writes one INFO line per action a client submits and per session a client
starts or restores, accepted or refused:

```
audit user=can credential=ticket credential_id=4c1f... action=run_tasks scope=etl session=etl-20260908-101459 keys=3 result=accepted
```

Under `LOG_JSON` the same fields travel as an `audit` object beside `message`, so a log store
queries them: `action`, `scope` (the scope name of the workflow), `session`, `keys` (the task count of a
run or check), `result`, and `reason` on a refusal. A read that changes nothing writes no audit line.
