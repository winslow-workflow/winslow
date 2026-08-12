# Changelog

All notable changes to Winslow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Winslow follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor
versions may include breaking changes).

## [Unreleased]

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
