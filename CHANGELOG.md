# Changelog

All notable changes to Winslow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Winslow follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor
versions may include breaking changes).

## [0.8.2] — 2026-10-28

### Fixed

- A session lists and resolves once its tasks are built. A roster read
  during the task build used to fail with a TypeError. The registry holds
  the session id during the build, so the manifests read hides a manifest
  under restore and a second restore of it is refused.

## [0.8.1] — 2026-10-28

### Changed

- A served process leaves the user out of the host stats. `HostStats.user` is
  `None`, and the system resources pane of the web UI shows the memory, the cpu
  and the host. The account of a served process is the server account, and the
  person who views the page logs in through the auth backend.

### Fixed

- The local TUI shows the uid number as the user when the account has no name,
  as in a container that runs under a uid without a passwd entry. The
  `host_stats` read used to fail there on every poll.

## [0.8.0] — 2026-10-24

### Added

- Nested workflows as a tree. `WorkflowDescriptor.parent` names the nearest
  listed workflow whose directory holds the directory of the workflow, `None`
  at the top level. The dashboard of the web UI lists a child indented under
  its parent, siblings in name order.
- `scopes` on an auth backend: the scopes a login at its authorization server
  asks for. The MCP resource metadata lists the scopes of the backend that
  names the authorization server, so an agent requests the same ones.
- `env` on the session manifest, the `ENV` of the process that started the
  session. A process lists and restores the sessions of its own environment
  alone, and a restore request for a session of another environment is
  refused with the environment to restore it from. A manifest without the
  stamp counts for every environment.
- The initialization log names the reason of a skipped task: the message of
  the `TaskSkip` its constraint raised.

### Changed

- The directories are scoped by environment: `STATE_DIR`, `CACHE_DIR` and
  `LOG_DIR` default to `.winslow/{env}/state`, `.winslow/{env}/cache` and
  `.winslow/{env}/logs`, where `{env}` resolves to `ENV` (`local` without
  one), so one checkout keeps the sessions, caches and logs of each
  environment apart. A settings module that sets a directory without `{env}`
  keeps it as is.

### Fixed

- An unselected optional choice on the web start form, sent as null, passes
  validation.

## [0.7.0] — 2026-10-14

### Added

- Per-user credentials and a policy of winslow's own. `.winslow/policy.toml`
  (`AUTH_POLICY_FILE`) holds groups, users and scopes, reloaded when it changes.
  `winslow auth token create <user>` mints a `wsl_` token whose SHA-256 hash
  lands in `.winslow/tokens` (`AUTH_TOKENS_FILE`), with `list`, `revoke`, `show`
  and `check` beside it. `winslow serve` logs the accepted credentials at
  startup and a WARNING for every grant of `*:*`.
- Auth backends, in the shape of Django's `AUTHENTICATION_BACKENDS`.
  `AUTH_BACKENDS` lists the credential kinds a server accepts, in order, and
  the first backend whose shape matches a credential decides. `identify` says
  who presented it, `scopes_for` what they may do, the policy file by
  default, and a deployment subclasses one backend to override the rule.
  `winslow.access.backends` holds `TokenBackend` and `TicketBackend`. A
  backend that fails answers a refusal, and the server log has the traceback.
  `winslow.contrib.oidc.OidcBackend` verifies the tokens of an OpenID Connect
  issuer against its key set, takes the user and the groups from the claims,
  and renews an expired token through the stored registration. With one
  listed, the MCP resource metadata names the issuer as the authorization
  server, so an agent logs in there by itself.
- `winslow auth login <url>` stores the credential of a server in
  `~/.config/winslow/credentials.toml` (`CREDENTIALS_FILE`): prompted without
  echo, from stdin with `--with-token`, or through the browser login with PKCE
  of the backend `--kind` names. `winslow connect` sends the stored
  credential, renews an expired one first, and records the user the server
  named. `logout` removes an entry, `status` lists them. A `ws://` URL to a
  host other than loopback needs `--insecure`.
- Tickets carry scopes. A ticket is a JWT signed with HS256 under
  `AUTH_TICKET_SECRET`: `mint_ticket(secret, user, scopes, audience, ttl)`
  spells what the user may do, and `aud` names the server (`SERVE_PUBLIC_URL`,
  required beside the secret). A ticket lives an hour at most, carries a
  `jti`, and travels as the `token` of the connection request and in the MCP
  bearer header.
- The permission check of the port. Every `Read` and every `Action`
  declaration names the permissions the caller needs on the scope of the
  session, and the three list reads take a `visible` filter.
  `create_session` and `restore_session` are actions of the app port: the
  action frame carries no session id, and the `SessionAck` carries the
  `SessionInfo`, or the traceback of a failed init in `detail`. A client
  calls an action like a read, `client.run_tasks(keys=...)`, and the action
  dataclasses are gone. A refusal is a refused ack or a `RequestError` with
  the reason, on the websocket and the MCP endpoint alike. A subscribe needs
  `view_task` on the session, a stop the permission of the batch kind, and a
  settled session ends on `check_task` too. A `winslow.audit` logger records
  one line per action and per session start.
- The permission context of a connection. `hello_ok` carries `scopes`, the
  expanded `<scope name>:<permission>` set of the five permissions
  (`start_workflow`, `end_workflow`, `run_task`, `check_task`, `view_task`),
  `credential`, the kind of credential, and `expires_at`. A workflow class
  names its scope with `permission_scope`, default the workflow name,
  inherited by subclasses; `SessionInfo`, `WorkflowDescriptor` and
  `ManifestInfo` carry it. The TUI and the web UI render the guarded controls
  disabled until the server grants them, and again after a drop. The
  standalone web page asks for a credential when the server refuses one, and
  keeps it for the tab. `winslow.protocol.closes` names the close codes: 4400
  malformed connection request, 4401 credential refused, 4402 credential
  expired, 4403 origin refused, 4408 no connection request in time.
  `winslow.access.permissions` holds `PermissionContext` and `expand`;
  `AppClient.permissions()` serves the context of a client.
- The credential lifetime on a live socket. The server closes a websocket
  when the credential behind it expires, with close code 4402 and the reason
  `credential expired - reconnect with a fresh one`. The web page fetches a
  fresh ticket from `credentialUrl` and reconnects, `winslow connect`
  renews its stored credential through its backend and reconnects, and both
  show a "credential renewed" notice. `ConnectionEvent` carries the close code of a drop.
  `AUTH_MAX_CONNECTION` caps every socket at one hour by default; a
  credential with an earlier expiry keeps its own.
- The records name the user and the credential. The serve process runs every
  action and read under the user of the connection, and every thread it
  starts for that work copies the scope. A batch record and its history row
  carry `user` beside the credential kind and the token or ticket id, shown
  as `can (token 9f86d081)` in the TUI and the web UI. The session manifest
  carries `user` and `ended_by`, a cache record and its entry info
  `written_by`, and every log record of the work `user`, `credential` and
  `credential_id`. A local client sets no user, so its records carry `None`,
  and a cache file from an earlier version reads with `written_by` as
  `None`. `winslow.task.context.current_user` and `scoped_permissions` are
  the scope; `StateStore.mark_ended` takes `ended_by`.
- The web UI: `winslow serve` hands out a browser UI at `/`, with the
  dashboard and the session screens of the terminal over the websocket
  endpoint. `web` is an endpoint of `--endpoints`, on by default beside `ws`;
  `--endpoints ws` serves the websocket alone. The Vue source lives in
  `web/`, the build travels inside the wheel, and `window.WINSLOW` configures
  a page a front application embeds: `wsUrl`, `credential`, `credentialUrl`
  (see `docs/serve.md`). The assets answer with CORS headers for the listed
  origins, so an embedding page on another origin loads them.
- The `host_stats` read of the port: memory, cpu load, hostname and user of
  the machine that runs the sessions, over the websocket and as an MCP tool.
  The system resources pane of the terminal reads it, so `winslow connect`
  shows the server, and the web UI gains the same pane on both screens.
- `SERVICE_NAME` names the deployment. The MCP server registers as
  `<SERVICE_NAME>-<ENV>`, so a client lists one entry per deployment and
  environment. The header of the TUI and the navbar of the web UI show the
  service and the environment in the color of the environment, with a text
  color that reads on it. `ENV_COLORS` adds or replaces the colors per
  environment name (see `docs/serve.md`).

### Changed

- One settings module, Django style. `settings.py` in the working directory,
  or the module `WINSLOW_SETTINGS_MODULE` names, holds every setting: the
  scalars, the secrets read the way its author chooses, and the code the
  process wires as dotted paths. `winslow.conf.settings` is the object every
  module reads, and `winslow.conf.template` the module to copy. Winslow
  reads no other environment variable. `AUTH_BACKENDS`, `STATE_STORE`,
  `TELEMETRY`, `TUI_PLUGINS` and `FILTER_PLUGINS` replace the backend
  registries, the `telemetry.py` scan, the plugin entry points and the
  `[tool.winslow]` table. A listed class loads at every start. `winslow
  settings` prints every setting with its source and what each path resolves
  to. `settings.env` is `ENV`, with `local` as its default in place of `dev`,
  and `SessionPersistenceAdapter` is `SessionPersistenceSubscriber`.
- A loopback bind passes every connection in the `local` env alone. With
  `ENV` set, every bind requires a credential, so a server behind its proxy
  on `127.0.0.1` reads the credentials like any other. `AUTH_OPEN = True`
  passes every connection with every permission, for development. A bind on
  every interface answers the web page under the hostname too, and the
  origin of `SERVE_PUBLIC_URL` is accepted beside `SERVE_ORIGINS`.
- `hello_ok` names a credential kind on every endpoint: `open` for a
  connection without a credential in open mode, where it was null. A `wsl_`
  token the store does not know refuses as an unknown token, and a token of
  no known shape names the credential kinds the server accepts.
- The workflow start form shows `clear_cache` as its only orchestrator
  option. The Tasks pane sets `dry_run`, `force_run`, `force_success` and
  `disable_concurrency` per batch.

### Removed

- Every `WINSLOW_*` environment variable but `WINSLOW_SETTINGS_MODULE`, the
  shared token of `winslow serve` and the one `winslow connect` read
  included: a client holds a per-user token, a ticket or an OIDC token, and a
  job stores its token with `winslow auth login --with-token`. The
  `user:expiry:signature` ticket format. The `winslow.tui_plugins` and
  `winslow.filter_plugins` entry point groups, the `[tool.winslow]` lists and
  the `autoload` flag. The `telemetry.py` file scan and the leaf rule: a
  listed configuration answers a handler or stops the start.

### Fixed

- `winslow serve` stops on Ctrl+C with a client connected and a batch running.
  The shutdown ends the MCP listen streams with their final frame, gives the
  remaining connections five seconds, then asks every running batch to stop
  and waits for its worker with the same bound.

## [0.6.0] — 2026-09-09

### Added

- Serve mode: `winslow serve` exposes the live sessions over a websocket
  protocol, and `--endpoints ws mcp` adds an MCP endpoint for agents. Both
  endpoints serve the same reads and actions as the local TUI. `WINSLOW_TOKEN`,
  `WINSLOW_TICKET_SECRET` and `WINSLOW_ORIGINS` set the credentials.
- `winslow connect ws://host:port`: the TUI over the wire. Connected clients
  share the live sessions: a run, a cache action or a session end on one
  terminal shows on every other.
- Sessions survive a restart: the engine restores its open manifests at
  startup, and `auto_init` workflows start with it. `check_ttl` and
  `TaskStatus.STALE` bound how long a restored success is trusted.
- `WINSLOW_LOG_JSON=1`: every subcommand writes one JSON object per log line,
  task logs included, stamped with session and task fields for a log store.
- The cache overview shows the dependencies of each entry with live states.
- The History pane filters by status.

### Changed

- Breaking for plugin authors: the TUI renders through the client API. Render
  contexts carry a `client` and value objects; the live workflow, store and
  orchestrator attributes are gone, and observers subscribe to the session bus
  (`Workflow.bus`) by event class in the place of `StoreListener`. See
  `docs/ui-plugins.md`.
- Breaking: tasks are named by `Task.identity_key`. `Task.uuid` is gone, and
  `TaskInfo.uuid` is `key`.
- Breaking: the batch flags are per submit, and `SetBatchOptions` is gone.
- Every `WINSLOW_*` setting reads through `winslow.settings`, the serve
  credentials included.
- rich is optional. A headless install depends on blinker, networkx and
  python-decouple only; a terminal with rich installed keeps the pretty
  tracebacks.

## [0.5.1] — 2026-08-17

### Added

- Cache observability (see `docs/caching.md`): `BaseCache.peek(name)` returns the storage record
  of an entry, `MISSING`, or `EntryState.COMPUTING` — with a bounded wait, no computation and no
  tier promotion — and `inspect()` returns one `CacheEntryInfo` projection per entry with its
  state (`COLD`, `WARM`, `STALE`, `COMPUTING`, `ERRORED`), write time, ttl, dependencies and
  storage label. `CacheListener` delivers the cache events (`on_entry_computed`,
  `on_entries_invalidated`, the eager population brackets, `on_entry_error`); subscribe with
  `CacheContainer.add_listener`. Containers also gained `caches()`, `clear_all()` and
  `populate_all()`.
- Error quarantine: a failed drop marks the entry ERRORED with a `CacheEntryError` (origin, tier,
  message and the traceback, formatted at failure time), keeps the record observable and forces
  the next read to recompute; the write then overwrites every writable tier and heals the entry.
  Invalidation completes its cascade on a broken tier instead of aborting. A failed loader marks
  the entry the same way, so the UI can tell a broken loader from a never-read entry.
- History capture of cache reads: each task phase records the entries the task read, rendered and
  bounded by `WINSLOW_CACHE_SNAPSHOT_SIZE_BYTES` (per-class override: `snapshot_size_bytes`). The
  task detail popup shows them next to the attributes; a TREE-styled snapshot stores JSON and
  opens as a tree from history.
- `display_style` on `@entry`: `DisplayStyle.RAW` (the default, bounded pretty-print),
  `DisplayStyle.TREE` (a lazily expanding tree) or a callable that formats the value. The
  declaration drives the live value view and the history snapshot.
- The Caches pane of the TUI: a Caches tab next to Tasks and History with one card per cache,
  entry rows with live states and value previews, search with the shared preview-then-filter
  flow, a scope filter, per-row view/load/clear and parallel load-all/clear-all. A cache detail
  tab accompanies it, and a value modal renders per the display style — an ERRORED entry shows
  its error context and stored traceback. Cache actions log through the session logger
  (`Session.log_scope`), and a cache whose storage cannot be observed degrades visibly instead of
  breaking the pane.
- UI plugin framework: `UIPlugin.should_render(context)` keeps a plugin out of a composition
  (the cache tabs hide in a project with no caches), and `detail_of` pairs a detail plugin with
  its master tabs — the screen brings the companion tab forward on a switch.

### Changed

- A cache name must start with a letter: a leading underscore now fails at collection.
- A cache name that matches a container member (`caches`, `inspect`, `clear_all`,
  `populate_all`, `populate_eager_entries`, `add_listener`, `remove_listener`, `listeners`) now
  fails at construction, and the entry names `peek`, `inspect`, `describe_storage`, `scope` and
  `snapshot_size_bytes` are reserved.
- `ComposedStorage.delete` attempts every writable tier, then raises one `StorageError` naming
  the failing tiers; the cache layer catches it and quarantines the entry.
- A storage backend must subclass `BaseStorage`, which carries the `peek`, `describe` and
  `read_only` defaults of the contract.
- A raising store or cache listener is logged with its traceback and skipped: an observer cannot
  break the operation it observes.
- The modal shell styles moved from the app stylesheet to `BaseModal.DEFAULT_CSS`, so a plugin
  modal overrides the shell with its own `DEFAULT_CSS`.

## [0.5.0] — 2026-08-14

### Added

- Declarative caching (see `docs/caching.md` and `examples/weather/`):
  `GlobalCache` (process scope) and `WorkflowCache` (session scope) classes,
  discovered from the `cache.py` and `cache/` locations of a project. Fields
  are declared with `@entry` (lazy, `eager`, `depends_on`, `ttl`), computed
  under a per-field lock, and stored behind a swappable `storage_class` seam.
  Eager fields populate in parallel before the graph is built, so
  `get_parameters` can read them through `winslow.cache.get_workflow_cache()`.
  Tasks, workflows and graphs expose the instances as `workflow_cache` and
  `global_cache` containers. A loader logs through `self.logger`, which
  resolves to the triggering task's log view, or to `winslow.runs.cache`
  outside a task scope.
- Cache invalidation: `invalidate(*names)` drops one or more entries and
  their declared dependents, transitively; `invalidate_all()` drops every
  entry. A `ttl` expiry and every invalidation log the dropped entries,
  attributed to the triggering task when one is in scope.
- Cache storage backends: `JsonFileStorage` persists one inspectable JSON
  file per entry (atomic writes, strict serialization, warm starts across
  processes), and `compose(*storage_classes)` layers backends into a tiered
  storage with read-through promotion and all-tier invalidation. The storage
  seam is record-based: `write(key, record)` returns the stored record, so a
  serializing backend serves its normalized values from the first write on.
  A custom backend subclasses `BaseStorage`, which states the contract.
  A storage class is constructed with `(cache_name, namespace)`; the files of
  a `GlobalCache` live under `<base>/global/<cache>/` and those of a
  `WorkflowCache` under `<base>/workflows/<identity>/<cache>/`, where the
  identity is `Workflow.cache_namespace`: the instance name plus the scalar
  identifier values, then a digest of the full identifier set. Two runs share
  records exactly when their whole identity matches.
  A backend signals `SerializationError` for a value that it cannot represent
  and `DeserializationError` for a record that it cannot decode; an undeclared
  read cycle or an invalidation from inside a loader raises
  `CacheReentrancyError` in the place of a silent deadlock.
- The `--clear-cache` run option: every cache entry of both scopes is
  invalidated at each workflow initialization, before the eager population,
  so a run starts from cold caches. Meaningful for persistent storage; a
  no-op on fresh memory caches.

### Changed

- `execute_in_threads` runs each call in a copy of the context of the caller,
  so transient properties, batch flags and log attribution resolve inside a
  task's own fan-out.
- `winslow/cache.py` became the `winslow/cache/` package. Every existing
  import path (`phase_cache`, `batch_cache` and the other transient helpers)
  is preserved through the package face.

### Fixed

- A session end now replaces a recorded batch error with a value-only copy
  (type and message), in the place of a traceback release. Exception
  attributes such as `AttributeError.obj` and the args no longer retain
  tasks past the session end.
- A buffered log record with an object in `extra` is coerced to its safe
  repr, so a custom log attribute cannot retain a task through the buffer.
- The status history of the test stores keys by the item uuid, so two items
  with one label no longer merge their histories.
- The completion sweep captures with the project root, so history shows the
  same source labels as the live task detail.
- The per-directory docs cache is cleared at session end, so an edited
  markdown doc renders fresh in the next session of one process.

## [0.4.0] — 2026-08-12

### Changed

- Execution history is task-free: batch records, execution events and history
  rows reference a task by `Task.uuid` and hold `TaskInfo` value snapshots,
  never the task. A session end now frees every task of the session, and the
  history stays browsable.
- `TaskInfo` is the complete, JSON-serializable view model of a task (identity,
  flags, parameters, dependency refs, attributes, docs, source). Dependency
  lists hold flat `TaskRef` values in the place of tasks.
- The task log dispatcher keys by `Task.uuid` in the place of `id(task)`, and
  buffered records are rendered to text, so a log buffer never retains a task.
- A session end releases the tracebacks of recorded batch errors, so a
  `--reraise-errors` run cannot retain tasks through history either.
- The execution-history search accepts only the builtin name and group filters
  and warns on a project filter (see the filter syntax docs).

### Breaking

- `TaskDetailRenderContext.task` is renamed to `info` and carries a `TaskInfo`
  value. A `winslow.tui_plugins` plugin that read `context.task` must read
  `context.info` and its value fields.
- `StoreListener.on_execution_status` and `on_log_appended` receive the task
  uuid in the place of the task object.
- The `ExecutionStatusChanged` and `TaskLogUpdated` UI messages carry
  `task_uuid` in the place of `task`. A plugin widget that handles them must
  route by the uuid.
- `TaskSelected.task_info` is a pure value: `.task` is gone, the dependency
  lists hold `TaskRef` values, `source_tree()` became the `source` field, and
  `groups` is a tuple (`groups_readable` renders the display form).

## [0.3.0] — 2026-08-10

### Added

- Error telemetry: errors report to Sentry (`winslow[sentry]`) and OpenTelemetry
  (`winslow[otel]`) when their environment values are set, with a `telemetry.py`
  seam for custom backends. See the telemetry docs.
- Settings resolve through python-decouple: a `.env` file in the working
  directory (or a parent) now works.

### Changed

- Log records carry the name and the instance of the workflow and the task.

## [0.2.0] — 2026-07-30

Initial public release.

Note: 0.1.0 was a placeholder upload that reserved the package name on PyPI.
0.2.0 is the first real release.
