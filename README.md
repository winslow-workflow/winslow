# Winslow

[![PyPI](https://img.shields.io/pypi/v/winslow)](https://pypi.org/project/winslow/)
[![Python](https://img.shields.io/pypi/pyversions/winslow)](https://pypi.org/project/winslow/)
[![CI](https://github.com/winslow-workflow/winslow/actions/workflows/ci.yml/badge.svg)](https://github.com/winslow-workflow/winslow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**A workflow and state manager that serves terminals and agents.**

Winslow runs work as a set of small tasks with declared dependencies. You drive
them from where you are:

- A live terminal dashboard on your machine, `winslow run`
- A terminal connected to a shared host, `winslow connect`
- An agent over MCP, `winslow serve --endpoints ws mcp`
- Headless in cron or CI, `winslow run --mode headless`

Every task knows two things: how to do its work, and how to tell whether that
work is already done. Winslow checks the second before it does the first, so a
second run of a workflow only does what still needs doing.

> If you have a pile of scripts held together with "did this step already run?"
> checks, Winslow is that pattern as a framework.

---

## Demo

https://github.com/user-attachments/assets/23426158-0447-421b-ab16-caf4c0d9103e

---

## Install

Each mode is one extra:

```bash
uv add "winslow[serve]"      # the shared host
uv add "winslow[mcp]"        # the shared host, with the MCP endpoint
uv add "winslow[tui]"        # one machine: the UI and the sessions together
uvx --from "winslow[connect]" winslow connect ws://host:8866   # a terminal, no project needed
```

Winslow needs Python 3.12 or later. `pip install 'winslow[serve]'` works the
same way. A headless run in cron or CI needs the bare `winslow` package only.

## Quick start

A workflow is a directory with a `Workflow` class and the `Task` classes that
belong to it. The file name `workflow.py` marks the directory as a workflow
package, and every `.py` file next to it belongs to that workflow. Put this in
`workflows/etl/workflow.py`:

```python
import os
from winslow import Workflow, Task


class Etl(Workflow):
    pass  # The name defaults to "etl", the kebab-cased class name.


class DownloadData(Task):
    def run(self):
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

Start the terminal UI from that directory:

```bash
winslow run
```

Or serve the directory and connect to it from another terminal:

```bash
winslow serve                                                        # on the host
uvx --from "winslow[connect]" winslow connect ws://127.0.0.1:8866    # from a terminal
```

Or run it headless, for cron and CI:

```bash
winslow run --mode headless --workflow etl
```

The [getting started](https://winslow-workflow.org/getting-started/) page walks
through all three. [Serve and connect](https://winslow-workflow.org/serve/)
covers a team on one host and an agent over MCP.

## The core idea: `run` and `check`

Two methods are the whole contract:

- `run(self)` makes a change to the world.
- `check(self)` reports whether the wanted end state already holds.

Before it runs a task, Winslow calls `check()`. If the check passes, the task is
skipped. If it fails, `run()` executes and Winslow calls `check()` again to
confirm the result. A workflow is therefore idempotent and resumable: interrupt
it halfway, run it again, and it continues from where it stopped. To force a
rerun, `winslow run --force-run` skips the pre-check.

A task that only verifies state can omit `run()`. It becomes a read-only check.

## Start as a status board and automate later

A workflow built only from checks changes nothing. It reads your systems and
reports what is true. That makes adoption incremental:

1. Describe the runbook you already have as checks, one `check()` per step,
   automated or manual. The TUI is now a live status board over your process
   as it exists today.
2. Add `run()` to one task at a time, wherever automation pays off. The rest
   stay checks, and the workflow is usable at every step in between.

A task can also automate part of its work and leave the rest to a person. The
[docs](https://winslow-workflow.org/#adopt-winslow-one-task-at-a-time) show how.

## What else is there

Each of these is optional. Start with `run` and `check` and add the rest when
you need it.

- **Dependencies and ordering.** Declare `dependencies = OtherTask`, a tuple, or
  a task group name. Winslow builds the dependency graph, detects cycles, and
  runs the tasks in order. Mark foundational steps `is_premier` and cleanup
  steps `is_terminal`.

- **Eligibility and guards.** Override `is_eligible()` to skip a task in some
  environments, or `can_run()` to block it until a precondition holds. A
  reusable rule is a constraint class:

  ```python
  class DeployTask(Task):
      runnability_constraints = [BusinessHoursOnly]
  ```

- **Parameterized tasks.** One task class fans out into many instances through
  `Parameter` declarations, for example one `ProcessRegion` task per region,
  each tracked and run on its own.

- **A filter language you can extend.** Narrow the view or a run with
  expressions like `build,test`, `!g deploy`, or `~lint & !group nightly`, in
  the search box of the UI or with `--filter` on the CLI. Subclass `TaskFilter`
  to add a `!command` of your own, and it works everywhere filters do.

- **Declarative caching.** Share expensive data such as station lists,
  calendars or reference tables through `GlobalCache` (process scope) and
  `WorkflowCache` (session scope) classes. Declare fields with `@entry` (lazy,
  eager, `depends_on`, `ttl`). Eager fields load in parallel before the graph
  is built, and `JsonFileStorage` keeps a cache warm across processes.

- **Error telemetry.** Report task and workflow errors to Sentry
  (`winslow[sentry]`) or OpenTelemetry (`winslow[otel]`) by setting their
  environment values. Each error reaches the backends once.

- **Live terminal UI.** Watch tasks change state, stream per-task logs, browse
  the execution history, and run or re-check single tasks by hand, all from the
  dashboard.

- **Serve and connect.** `winslow serve` starts the engine, the component that
  owns the sessions, and exposes it over a websocket. Every connected terminal
  sees the same sessions. `--endpoints ws mcp` adds an MCP endpoint, so an
  agent lists, starts, runs and inspects sessions with the same reads and
  actions a terminal has.

- **A pluggable UI.** The dashboard and the workflow screen are assembled from
  plugins that fill named slots. Subclass `UIPlugin` to add a tab or a panel,
  or to replace a built-in one. UI plugins and filters can live in their own
  packages, found through entry points (`winslow.tui_plugins`,
  `winslow.filter_plugins`).

## CLI at a glance

```bash
winslow serve                                  # the sessions on 127.0.0.1:8866
winslow serve --host 0.0.0.0 --endpoints ws mcp    # on the network, with /mcp for agents
winslow connect ws://host:8866                 # the TUI against a server
winslow run                                    # the UI and the sessions in one process
winslow run --workflow etl                     # UI, pre-selecting a workflow
winslow run --mode headless --workflow etl     # headless run
winslow run --mode headless --check ...        # check completion without running
winslow run --dry-run ...                      # call dry_run() instead of run()
winslow run --clear-cache ...                  # invalidate every cache entry first
winslow show                                   # list workflows
winslow show --initialize --workflow etl       # initialize one workflow, list its tasks
winslow show --initialize --workflow etl --with-deps   # ...with each task's dependencies
```

Winslow discovers the workflows from the current directory. An `Orchestrator`
subclass that adds global options is optional.

## How it compares

Winslow runs on one host. There is no scheduler daemon and no metadata
database. Done-ness is whatever `check()` observes right now, a file, a table,
an API response, a merged PR, so when the world drifts, the next run sees it.
Tasks run where the engine runs, on your laptop under `winslow run` or on the
shared host under `winslow serve`, and cron or CI triggers the unattended runs.

If you need distributed workers, a built-in scheduler, or a central run history,
a different tool fits better. The
[tool selector](https://winslow-workflow.org/selector.html) compares Winslow
with the common alternatives under the same rules. Tick what your situation
requires and see what fits.

## Trust model

Treat a workflow directory like a `Makefile` or a `conftest.py`: running
`winslow` in a directory runs the code of that directory, imported at startup
before any prompt. Plugin and filter autodiscovery is opt-out as well. An
installed package with a winslow entry point loads on startup, and the
[plugin guide](https://winslow-workflow.org/plugins/) shows how to constrain or
allowlist them. A serve credential grants every action on the serving host,
including starting a workflow, which runs its code. The full trust model and
the vulnerability reporting process are in [SECURITY.md](SECURITY.md).

## Status

Winslow is early, 0.x, and the API may still change between minor versions.
The full documentation lives at [winslow-workflow.org](https://winslow-workflow.org).

## Questions and feedback

- Questions, use cases and ideas:
  [Discussions](https://github.com/winslow-workflow/winslow/discussions)
- Reproducible bugs:
  [Issues](https://github.com/winslow-workflow/winslow/issues)

Real use cases are welcome while the API is still settling. What you automate,
and what fought you, both shape what 0.x becomes.

## License

[MIT](LICENSE)
