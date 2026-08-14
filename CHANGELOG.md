# Changelog

All notable changes to Winslow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Winslow follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor
versions may include breaking changes).

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
