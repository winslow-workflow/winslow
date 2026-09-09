# Serve and connect

A workflow that matters to a team runs on a machine the team shares: a build host, a bastion, a
container next to the data. `winslow serve` runs the engine there, and everyone works against it
from their own terminal with `winslow connect`. The sessions live on the host, so a batch keeps
running when a laptop closes, two people watch the same run, and the History tab is the record of
what the team did. With `--endpoints ws mcp` the same host also serves agents over MCP, with the
same reads and actions a terminal has.

Compared to `winslow run`, nothing changes in the workflow code or in the UI. The engine moves out
of the terminal into its own process, and the terminal becomes one client of it.

The page starts with the two setups most people want, [a team on one host](#a-team-shares-one-host)
and [an agent over MCP](#connect-an-agent). The rest is the reference: the
[process picture](#the-process-picture), the [extras](#install), the [serve](#start-a-server) and
[connect](#connect-a-terminal) commands, the [credential rules](#credentials), the
[MCP endpoint](#the-mcp-endpoint) and the [log stream](#logs).

## A team shares one host

The engine is the place where the tasks run. Everything else is a client of it. The host runs the
server on the network with a token in its environment:

```bash
export WINSLOW_TOKEN=a-long-random-string
winslow serve --host 0.0.0.0
```

Each engineer connects with the same token in their own environment:

```bash
export WINSLOW_TOKEN=a-long-random-string
uvx --from "winslow[connect]" winslow connect ws://etl-host:8866
```

Both see the same sessions. A batch one engineer starts shows its task states and its log lines on the
other screen as they happen, and the History tab of both lists it when it completes.

## Connect an agent

An agent gets the same reads and actions as a terminal, over MCP. On the host:

1. Install `winslow[mcp]` on the host (see [Install](#install)).

2. Serve both endpoints:

    ```bash
    winslow serve --endpoints ws mcp
    ```

    The MCP endpoint is at `http://127.0.0.1:8866/mcp`. On a shared host the server binds the network
    and the agent sends `WINSLOW_TOKEN` as a bearer header (see [Credentials](#credentials)).

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
          "headers": {"Authorization": "Bearer a-long-random-string"}}'
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
| `winslow[serve]` | starlette, uvicorn, websockets, pydantic | the server |
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
winslow serve                              # 127.0.0.1:8866, websocket at /ws
winslow serve --host 0.0.0.0 --port 9000   # reachable from other hosts
winslow serve --endpoints ws mcp               # adds the MCP endpoint at /mcp
winslow serve --endpoints mcp                  # MCP only
```

The engine reads the workflows of the working directory, like `winslow run`. At startup it
rebuilds every open manifest of the state directory, so the sessions of a dead process come back
before the first client connects (see [Sessions and restore](sessions.md#restore-in-serve-mode)).
A workflow with `auto_init = True` starts one session in the engine at the same point.

## Connect a terminal

A terminal needs no project. `uvx` fetches the client and runs it:

```bash
uvx --from "winslow[connect]" winslow connect ws://127.0.0.1:8866
WINSLOW_TOKEN=... uvx --from "winslow[connect]" winslow connect ws://build-host:8866
```

A machine that connects often adds `winslow[connect]` to a project and runs `winslow connect` from
there. The URL is the one positional argument. The dashboard and the workflow screens are the ones of
`winslow run`, drawn from the snapshots the server sends. Every action goes to the engine, and
the task logs stream back over the socket.

## Credentials

The server reads three settings from the environment of its process:

| setting | holds |
|---|---|
| `WINSLOW_TOKEN` | the bearer token of machine clients: `winslow connect`, scripts, MCP agents |
| `WINSLOW_TICKET_SECRET` | the secret that signs the tickets of browser clients |
| `WINSLOW_ORIGINS` | the browser origins the websocket endpoint accepts, comma separated |

```bash
export WINSLOW_TOKEN=a-long-random-string
export WINSLOW_TICKET_SECRET=another-long-random-string
export WINSLOW_ORIGINS=https://ops.acme.internal,http://localhost:5173
```

During development a `.env` file in the working directory sets the same variables, like every
other `WINSLOW_*` setting. An exported variable wins over the file.

The rules:

- A loopback bind needs no credential. `127.0.0.1`, `::1` and `localhost` accept every hello.
  Every process on the machine reaches a loopback server.
- Every other bind needs a credential on every hello. A token or a ticket. The MCP endpoint
  on such a bind needs `WINSLOW_TOKEN` set before the server starts.
- A browser origin must be listed. A hello that carries an `Origin` header passes when the
  header value is in `WINSLOW_ORIGINS`. This holds on every bind, the loopback one included, so a
  web page in the operator's browser reaches a local server only when its origin is listed. A
  client without a browser sends no `Origin` header and is unaffected.
- A credential grants every action on the host. There is one role. A client with a token
  starts sessions, which imports and runs the project code, runs and stops tasks, ends sessions
  and clears caches. Task details carry tracebacks and absolute paths of the host. Hand a token
  to the people you would give a shell on that host.

An origin is scheme, host and port, with no path and no trailing slash. `http://localhost:5173`
and `http://127.0.0.1:5173` are two origins.

The engine holds every session and the server every connection a client asks for. Size the host
for the clients you expect.

### Tickets for browsers

A browser client authenticates against your front application, which mints a ticket and hands it
to the page. `winslow.serve.mint_ticket` and the server share one rule:

```python
from winslow.serve import mint_ticket

ticket = mint_ticket(secret, user="can", ttl=60.0)   # user:expiry:signature
```

The page opens the socket and sends the ticket in its hello. The server verifies the signature
under `WINSLOW_TICKET_SECRET` and the expiry, and answers `hello_ok` with the user of the ticket.

## The websocket handshake

The first message on the socket is a hello. A machine client sends the token and a browser sends
the ticket:

```json
{"type": "hello", "token": "a-long-random-string"}
```

The server answers `hello_ok` with the user it resolved:

```json
{"type": "hello_ok", "user": "token-client"}
```

A refused hello receives a `hello_error` frame with the reason, then the socket closes with a
code that names the refusal class:

| code | refusal |
|---|---|
| 4400 | the first message is not a hello |
| 4401 | the credential or the origin is refused |
| 4408 | no hello within the timeout |

After `hello_ok` the client subscribes to sessions and submits actions over the same socket.
`winslow.protocol.frames` declares every frame, and `winslow.client.websocket` is the reference
client.

## The MCP endpoint

`--endpoints mcp`, alone or beside `ws`, mounts an MCP server at `/mcp` over streamable HTTP. It is the
tool layer over the same port: the tools read snapshots and submit actions, and every action tool
answers the ack as data, accepted with the batch uuid or refused with the reason, so an agent reads
the outcome as a value.

| group | tools |
|---|---|
| discover | `list_sessions`, `descriptors`, `manifests` |
| sessions | `start_session`, `restore_session`, `end_session` |
| read | `snapshot`, `roster`, `task_detail`, `history`, `record_detail` |
| act | `run_tasks`, `check_tasks`, `stop_batch`, `load_cache_entries`, `clear_cache_entries` |

`descriptors` lists the workflows the engine can start with their options. Its rows fill the
`values` of `start_session`. `run_tasks` and `check_tasks` take task identity keys from
`snapshot` or `roster`, plus the batch flags as `options`, for example `{"force_run": true}`.

An MCP client on a non-loopback bind sends the token as a bearer:

```json
{
  "mcpServers": {
    "winslow": {
      "url": "http://build-host:8866/mcp",
      "headers": {"Authorization": "Bearer a-long-random-string"}
    }
  }
}
```

## Logs

The default keeps the log files of `winslow run`: one file per session under `WINSLOW_LOG_DIR`.
`WINSLOW_LOG_JSON=1` sends one JSON object per record to stdout instead, task logs included, each
stamped with the session and task fields, so a pod writes one stream for its log store:

```bash
WINSLOW_LOG_JSON=1 winslow serve --host 0.0.0.0
```

```json
{"ts": "2026-09-08 10:15:02 UTC", "level": "INFO", "session_id": "etl-20260908-101459",
 "workflow_name": "etl", "workflow_instance": "etl", "task_name": "DownloadData",
 "task_instance": "DownloadData", "batch_uuid": "5f1c...", "message": "downloaded 3 files",
 "traceback": null}
```

The uvicorn lines share the same handler, so the whole process writes one format.
