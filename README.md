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
    is_premier = True  # foundational step, runs first

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
winslow run --mode headless --workflow etl # headless run
winslow run --mode headless --check ...    # check completion without running
winslow run --dry-run ...                      # call dry_run() instead of run()
winslow show                                   # list workflows
winslow show --initialize --workflow etl      # initialize one workflow, list its tasks
winslow show --initialize --workflow etl --with-deps   # ...with each task's dependencies
```

Winslow discovers your workflows from the current directory; defining an
`Orchestrator` subclass to customize global options is optional.

## Trust model

Treat a workflow directory like a `Makefile` or a `conftest.py`: **running
`winslow` in a directory runs that directory's code.** On startup Winslow
imports each workflow's `workflow.py` (and every `.py` beside it), plus the
top-level modules used for orchestrator discovery — at import time, before any
prompt. Only run winslow in directories you trust.

Plugin and filter autodiscovery is **opt-out, not opt-in**: any installed
package exposing `winslow.tui_plugins` / `winslow.filter_plugins` entry points is imported
and its `autoload = True` classes registered on startup. Constrain this from
your `pyproject.toml`:

```toml
[tool.winslow]
disabled_tui_plugins = ["some-plugin"]     # or enabled_tui_plugins to allowlist
disabled_filter_plugins = ["some-filter"]     # likewise enabled_filter_plugins
```

## Status

Winslow is early (0.x) — the API may still shift between minor versions. Full
documentation lives at [winslow-workflow.org](https://winslow-workflow.org).

## License

[MIT](LICENSE)
