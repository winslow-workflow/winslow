# Winslow

[![PyPI](https://img.shields.io/pypi/v/winslow)](https://pypi.org/project/winslow/)
[![Python](https://img.shields.io/pypi/pyversions/winslow)](https://pypi.org/project/winslow/)
[![CI](https://github.com/winslow-workflow/winslow/actions/workflows/ci.yml/badge.svg)](https://github.com/winslow-workflow/winslow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**A state and workflow management framework with a terminal UI.**

Winslow lets you describe work as a set of small, dependency-aware **tasks** and
then run them from a live terminal dashboard — or headless in CI. Its guiding
idea is simple: every task knows both **how to do its work** and **how to tell
whether that work is already done**. Winslow checks the latter before doing the
former, so re-running a workflow only does what still needs doing.

> If you've ever written a pile of scripts held together with "did this step
> already run?" checks, Winslow is that pattern, made first-class.

---

## Demo

https://github.com/user-attachments/assets/23426158-0447-421b-ab16-caf4c0d9103e

---

## Install

```bash
uv add "winslow[tui]"      # or: pip install 'winslow[tui]'
```

Requires Python 3.12+. The `tui` extra pulls in the terminal UI; for headless
runs only (cron, CI), the bare `winslow` package is enough.

## Quick start

A workflow is a directory containing a `Workflow` class and the `Task` classes
that belong to it. The `workflow.py` filename marks the directory as a
workflow package — every `.py` file next to it belongs to that workflow.
Drop this in `workflows/etl/workflow.py`:

```python
import os
from winslow import Workflow, Task


class Etl(Workflow):
    pass  # name defaults to "etl" (kebab-cased class name)


class DownloadData(Task):
    def run(self):
        # ... fetch the file ...
        download("s3://bucket/raw.csv", "/data/raw.csv")

    def check(self):
        return os.path.exists("/data/raw.csv")


class TransformData(Task):
    dependencies = DownloadData

    def run(self):
        transform("/data/raw.csv", "/data/clean.csv")

    def check(self):
        return os.path.exists("/data/clean.csv")
```

Then launch the terminal UI from that directory:

```bash
winslow run
```

Or run it headless (handy for cron and CI):

```bash
winslow run --mode headless --workflow etl
```

## The core idea: `run` + `check`

These two methods are the whole contract:

- **`run(self)`** makes a change to the world.
- **`check(self)`** reports whether the desired end state already holds.

Before running a task, Winslow calls `check()`. If it's already true, the
task is skipped; otherwise `run()` executes and Winslow calls `check()`
again to confirm success. That makes workflows **idempotent and resumable** by
construction — interrupt one halfway and re-run it, and it picks up where it
left off. (Need to force a redo? `winslow run --force-run` skips the pre-check.)

A task that only verifies state can omit `run()` entirely — it becomes a
read-only **check**.

## Start as a status board, automate later

Because `run()` is optional, a workflow built only from checks changes
nothing — it just reads your systems and reports what's true. That makes
adoption incremental:

1. Describe the runbook you already have as checks — one `check()` per step,
   automated or manual. The TUI is now a live status board over your process
   exactly as it exists today. Zero migration.
2. Add `run()` to one task at a time, wherever automation pays off. The rest
   stay checks, and the workflow is usable at every step in between.

A task can even automate part of its work and leave the rest to a human —
see [the docs](https://winslow-workflow.org/#adopt-winslow-one-task-at-a-time)
for how that plays out.

## A few things you'll probably reach for

Each of these is optional — start with `run`/`check` and add the rest as
you need it.

- **Dependencies & ordering.** Declare `dependencies = OtherTask` (or a tuple /
  task-group name). Winslow builds the dependency graph, detects cycles, and
  runs things in the right order. Mark foundational steps `is_premier` and
  cleanup steps `is_terminal`.

- **Eligibility & guards.** Override `is_eligible()` to skip a task in some
  environments, or `can_run()` to block it until a precondition holds. For
  reusable rules, attach **composable constraints**:

  ```python
  class DeployTask(Task):
      runnability_constraints = [BusinessHoursOnly]
  ```

- **Parameterized tasks.** One task class can fan out into many instances via
  `Parameter` declarations — e.g. one `ProcessRegion` task per region — each
  tracked and run independently.

- **A task filter language — that you can extend.** Narrow the view (or a run)
  with expressions like `build,test`, `!g deploy`, or `~lint & !group nightly`,
  in the UI's search box or via `--filter` on the CLI. Add your own commands by
  subclassing `TaskFilter`: define a `!command` and what it matches, and it's
  available everywhere filters are.

- **Declarative caching.** Share expensive data — station lists, calendars,
  reference tables — through `GlobalCache` (process scope) and `WorkflowCache`
  (session scope) classes. Declare fields with `@entry` (lazy, eager,
  `depends_on`, `ttl`); eager fields load in parallel before the graph is
  built, and `JsonFileStorage` keeps a cache warm across processes.

- **Error telemetry.** Report task and workflow errors to Sentry
  (`winslow[sentry]`) or OpenTelemetry (`winslow[otel]`) by setting their
  environment values — each error reaches the backends exactly once.

- **Live terminal UI.** Watch tasks change state in real time, stream per-task
  logs, browse execution history, and run or re-check individual tasks by hand —
  all from the dashboard. Everything also works headless in `--mode headless`.

- **A pluggable UI.** The dashboard and workflow views are assembled from
  plugins that fill named slots. Add your own tab or panel — or replace a
  built-in one — by subclassing `UIPlugin`, no fork required. Both UI plugins
  and custom filters can be autodiscovered or shipped as separate packages via
  entry points (`winslow.tui_plugins`, `winslow.filter_plugins`).

## CLI at a glance

```bash
winslow run                                    # interactive terminal UI
winslow run --workflow etl                     # UI, pre-selecting a workflow
winslow run --mode headless --workflow etl     # headless run
winslow run --mode headless --check ...        # check completion without running
winslow run --dry-run ...                      # call dry_run() instead of run()
winslow run --clear-cache ...                  # invalidate every cache entry first
winslow show                                   # list workflows
winslow show --initialize --workflow etl       # initialize one workflow, list its tasks
winslow show --initialize --workflow etl --with-deps   # ...with each task's dependencies
```

Winslow discovers your workflows from the current directory; defining an
`Orchestrator` subclass to customize global options is optional.

## How it compares

Winslow is a **local-first** workflow tool. There is no server, no scheduler
daemon, and no metadata database: done-ness is whatever `check()` observes
right now — a file, a table, an API response, a merged PR — so if the world
drifts, the next run sees it. Tasks run where you invoke `winslow`, and cron
or CI is the intended trigger for unattended runs.

If your situation calls for distributed workers, a built-in scheduler, or a
central run history, a different tool will serve you better. The
[tool selector](https://winslow-workflow.org/selector.html) compares Winslow
with the common alternatives under the same rules — tick what your situation
requires and see what fits.

## Trust model

Treat a workflow directory like a `Makefile` or a `conftest.py`: **running
`winslow` in a directory runs that directory's code**, imported at startup
before any prompt. Plugin and filter autodiscovery is likewise **opt-out** —
installed packages exposing winslow entry points load on startup; the
[plugin guide](https://winslow-workflow.org/plugins/) shows how to constrain
or allowlist them. The full trust model and the vulnerability reporting
process are in [SECURITY.md](SECURITY.md).

## Status

Winslow is early (0.x) — the API may still shift between minor versions. Full
documentation lives at [winslow-workflow.org](https://winslow-workflow.org).

## Questions & feedback

- Questions, use cases, and ideas →
  [Discussions](https://github.com/winslow-workflow/winslow/discussions)
- Reproducible bugs →
  [Issues](https://github.com/winslow-workflow/winslow/issues)

Real-world use cases are especially welcome while the API is still settling —
what you automate, and what fought you, both shape what 0.x becomes.

## License

[MIT](LICENSE)
