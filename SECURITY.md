# Security Policy

## Trust model (read this first)

Winslow runs the code in the directory you point it at. Treat a workflow
directory like a `Makefile` or a `conftest.py`: **running `winslow` in a
directory executes that directory's code** — each workflow's `workflow.py` and
the `.py` files beside it, plus modules used for orchestrator discovery, are
imported at startup, before any prompt.

The settings module is code too: `settings.py` in the working directory, or the
module `WINSLOW_SETTINGS_MODULE` names, is imported at startup, and the classes
it lists in `TUI_PLUGINS`, `FILTER_PLUGINS`, `TELEMETRY`, `STATE_STORE` and
`AUTH_BACKENDS` load with it.

Only run winslow in project directories and with dependencies you trust. This is
by design and standard for the category (make, tox, pytest) — it is not itself a
vulnerability.

## Serve mode

`winslow serve` exposes the sessions of one process over a websocket and, with
`--endpoints mcp`, over an MCP endpoint. The rules, in full on the
[serve page](https://winslow-workflow.org/serve/):

- The default bind is `127.0.0.1:8866`. A loopback bind in the default `local`
  env accepts every connection without a credential, since every process on
  the machine reaches it. A deployment sets `ENV`, so a loopback bind behind
  its proxy requires a credential like any other bind.
- Every other bind requires a credential on every connection, from the
  backends `AUTH_BACKENDS` lists: a `wsl_` token minted by `winslow auth token
  create`, a ticket signed with `AUTH_TICKET_SECRET` for a browser page, or a
  token of an OpenID Connect issuer. The MCP endpoint reads the same
  credentials as bearer headers.
- `AUTH_OPEN = True` passes every connection with every permission and logs a
  WARNING at startup. It is for development.
- A browser origin must be listed in `SERVE_ORIGINS`, on every bind.
- What a credential may do comes from the policy file: the scopes of its user
  and groups. `*:*` grants every action, `start_session` included, which
  imports and runs the project code on the serving host, and the task details
  carry tracebacks and absolute paths of that host. The startup log warns for
  every grant of `*:*`. Hand it to the people you would give a shell on the
  host.
- A credential holds a socket for `AUTH_MAX_CONNECTION` at most, one hour by
  default, so a change of the policy reaches a live connection within one
  lifetime.

## Reporting a vulnerability

If you find a security issue that goes beyond the trust model above (e.g. code
execution from data that shouldn't be trusted, path traversal, a way to bypass
the plugin allow/deny lists), please report it privately:

- Use GitHub's **private vulnerability reporting** (Security tab → "Report a
  vulnerability") on the repository, or
- Email **ocanbascil@gmail.com** with a description and reproduction.

Please don't open a public issue for a suspected vulnerability until it's been
triaged. Reports get acknowledged, investigated, and coordinated toward a fix
and disclosure.

## Supported versions

Winslow is pre-1.0; fixes land on the latest `0.x` release.
