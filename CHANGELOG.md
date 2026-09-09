# Changelog

All notable changes to Winslow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Winslow follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor
versions may include breaking changes).

## [Unreleased]

### Added

- Serve mode: `winslow serve` exposes the live sessions over a websocket
  protocol, and `--mcp` adds an MCP endpoint for agents. Both doors serve the
  same reads and actions as the local TUI.
- `winslow connect ws://host:port`: the TUI over the wire. Connected clients
  share the live sessions: a run, a cache action or a session end on one
  terminal shows on every other.
- Sessions survive a restart: the serve process restores its open manifests at
  startup, and `auto_init` workflows start with the server, not per client.
- `WINSLOW_LOG_JSON=1`: the serve process writes one JSON object per log line,
  task logs included, stamped with session and task fields for a log store.
- The cache overview shows the dependencies of each entry with live states.

### Changed

- rich is optional. A headless install depends on blinker, networkx and
  python-decouple only; a terminal with rich installed keeps the pretty
  tracebacks.
