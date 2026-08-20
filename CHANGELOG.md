# Changelog

All notable changes to Winslow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Winslow follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor
versions may include breaking changes).

## [Unreleased]

### Added

- `Task.identity_key`: a stable, session-durable identity string (the instance name plus a digest of the
  parameter reprs). Two builds of one task produce one key, so history, events and a future wire protocol
  can name a task without holding it. `Workflow.task_index` resolves a key back to the live task of the
  session through weak references, and it raises `IdentityKeyCollisionError` when two live tasks resolve
  to one key.
- The payload rule for UI plugins (see `docs/ui-plugins.md`): a Textual message and a store-listener
  callback carry the identity key and `TaskInfo` values. `WorkflowRenderContext.task_statuses` carries
  the `{key: TaskStatus}` mapping that the screen maintains.
- Session persistence and restore (see `docs/sessions.md`). A `StateStore` adapter persists one
  directory per session: the manifest, one snapshot file per task with its latest terminal status, and
  one record per batch with the option snapshot, the task roster, and a log dump at the close.
  `FileStateStore` is the default backend: live sessions under `WINSLOW_STATE_DIR/open` (default
  `.winslow/state`), ended sessions archived under `ended/`, and `WINSLOW_STATE_BACKEND` selects a
  backend that `register_state_backend` registered. The TUI dashboard lists the open manifests in a
  Restore pane and rebuilds a session under its original id: terminal statuses seed from the snapshots,
  mid-flight batches land in history as `ExecutionStatus.INTERRUPTED`, and their unsettled tasks come
  back `READY_TO_PROCESS`.
- `check_ttl`: a workflow-level default with a per-task override, in seconds. A passing snapshot younger
  than the effective TTL counts as verified wherever a check would run, without a probe. Snapshots are
  session-scoped, so the trust window spans a kill and a restore of the same session and never leaks
  into another session. The default `None` keeps today's behavior: always probe.
- `TaskStatus.STALE`: a passing status beyond its trust window turns STALE. A sweeper thread flips a
  status whose TTL lapses live, a restore seeds an untrusted success as STALE, and the next touch
  re-verifies it. The snapshot keeps the real outcome; `TaskInfo` carries `checked_at` and
  `effective_ttl` for the detail modal.
- The History pane filters by status: a dropdown beside the record search narrows the rows to one task
  status, and composes with the search and the hide-completed toggle.

### Changed

- Breaking for plugin authors: `TaskStatusChanged` carries `(key, status)`; it carried the live task.
  `ExecutionStatusChanged` and `TaskLogUpdated` name the task with `task_key`; the attribute was
  `task_uuid`.
- Breaking for plugin authors: `StoreListener.on_task_status(key, status)` receives the identity key.
  `on_execution_status` and `on_log_appended` receive it as `task_key`. Every listener payload is a value.
- Breaking for plugin authors: the Task Overview pane receives the statuses-by-key mapping
  (`WorkflowRenderContext.task_statuses`); it received the live store.
- `TaskInfo.uuid` and `TaskRef.uuid` are renamed to `key`, and the value is the identity key. Equality
  and hash follow the key. `LogContext.task_uuid` is renamed to `task_key` and carries the in-process
  log routing key (`Task.log_key`, a per-run nonce plus the identity key).
- Execution history keys by the identity key: `ExecutionRecordStore`, `ExecutionBatch.errored` and the
  status history of the store all hold identity keys.
- The Caches pane is unchanged: its rows keep the live `BaseCache` objects, which are process-local UI
  state.

### Removed

- `Task.uuid`. The identity key replaces it everywhere: log routing uses `Task.log_key`, and everything
  session-durable uses `Task.identity_key`.

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
