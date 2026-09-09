# Getting started

Install Winslow, write a two-task workflow, run it on your laptop, then serve the same directory for
other terminals. The page ends with the workflow options and the trust model.

## Install

Winslow needs Python 3.12 or later. Each mode installs one extra, shown with the mode in the quick start:

| extra | adds |
|---|---|
| `winslow[tui]` | the terminal UI on one machine, `winslow run` |
| `winslow[serve]` | the server, `winslow serve`, see [Serve and connect](serve.md) |
| `winslow[connect]` | the remote terminal, `winslow connect`, or `uvx --from "winslow[connect]"` with no project |
| `winslow[mcp]` | the server with its MCP endpoint |
| `winslow[sentry]`, `winslow[otel]` | error telemetry, see [Telemetry](telemetry.md) |

For a headless run in cron or CI, the `winslow` package alone is sufficient.

## Quick start

A workflow is a directory. It holds a `Workflow` class and the `Task` classes that belong to that workflow.
The filename `workflow.py` marks the directory as a workflow package. Every `.py` file beside it belongs to
the same workflow.

Put this content in `workflows/etl/workflow.py`:

```python title="examples/etl/workflow.py"
--8<-- "examples/etl/workflow.py"
```

[Download this example](https://github.com/winslow-workflow/winslow/blob/main/examples/etl/workflow.py)

!!! tip "Project layout"

    The example holds the workflow and the tasks in one file, which keeps it short. A real workflow puts the
    tasks in their own files beside `workflow.py`. Winslow imports every `.py` file in the directory. See
    [Workflows](workflows.md).

### Run it on your laptop

=== "uv"

    ```bash
    uv add "winslow[tui]"
    ```

=== "pip"

    ```bash
    pip install 'winslow[tui]'
    ```

Start the terminal UI from that directory:

```bash
winslow run
```

The dashboard lists the etl workflow. Start it, and the workflow screen shows the two tasks change state as
they run. Run the workflow a second time from the same screen. Winslow runs neither task, because `check()`
already returns true.

A headless run takes the workflow name and runs it to the end:

```bash
winslow run --mode headless --workflow etl
```

### Serve the same directory

The same workflow can run on a shared machine, where anyone on the team connects to it from their own
terminal. Install the server on that machine:

=== "uv"

    ```bash
    uv add "winslow[serve]"
    ```

=== "pip"

    ```bash
    pip install 'winslow[serve]'
    ```

Serve the directory:

```bash
winslow serve
```

The server listens on `127.0.0.1:8866`. A terminal needs no project and no install, one command connects:

```bash
uvx --from "winslow[connect]" winslow connect ws://127.0.0.1:8866
```

You get the same dashboard as `winslow run`, and everyone connected sees the same sessions. A machine
that connects often can add `winslow[connect]` to a project and run `winslow connect` from there.

## Pass options to a workflow

A workflow declares its runtime options with `ConfigOption`. Each option becomes a command line argument. A
task reads the values from `self.workflow_config`.

```python title="examples/report/workflow.py"
--8<-- "examples/report/workflow.py"
```

[Download this example](https://github.com/winslow-workflow/winslow/blob/main/examples/report/workflow.py)

Pass each value on the command line:

```bash
winslow run --mode headless --workflow report --region eu --limit 5
```

An option name uses an underscore in Python and a dash on the command line. The option `max_rows` thus becomes
`--max-rows`.

The `region` option is required. Winslow stops before it runs a task if the command omits the value:

```
Workflow - report: error: the following arguments are required: --region
```

The `limit` option has a default, so the command can omit it. The `choices` list is also enforced:

```
Workflow - report: error: argument --region: invalid choice: 'xx' (choose from eu, us)
```

The terminal UI presents the same options as a form. Fill the form in, then start the workflow.

![The workflow form, with a field for each config option](images/workflow-form.svg)

!!! info "The environment"

    Winslow reads the environment name from the `WINSLOW_ENV` variable, and the default value is `dev`. A task
    and a workflow both read the value from `self.env`.

## Trust model

!!! warning "Winslow runs the code in the directory that you start it from"

    Treat a workflow directory like a `Makefile` or a `conftest.py`. Start Winslow only in a directory that
    you trust.

At startup Winslow searches the current directory and every subdirectory below it for a `workflow.py` file. It
then imports each `workflow.py` file, every other `.py` file in the same directory tree, and the top-level
modules for the orchestrator discovery. Winslow ignores a directory whose name starts with a full stop or an
underscore. These imports happen before the first prompt.

A serve credential grants the same on the serving host: a connected terminal or agent starts sessions, which
runs the project code there. Plugin autodiscovery is also opt-out. An installed package that exposes a
winslow entry point loads at startup. The [plugin guide](plugins.md) shows how to constrain the discovery. The
[security policy](https://github.com/winslow-workflow/winslow/blob/main/SECURITY.md) describes the full
trust model and the report process for a vulnerability.
