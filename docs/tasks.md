# Tasks

A task is one unit of work. It knows how to do the work, and how to report that the work is already done.

## The contract

Two methods form the contract:

- `run()` makes a change.
- `check()` reports whether the wanted end state is already true.

Winslow calls `check()` before it runs a task. If `check()` returns true, Winslow marks the task completed and
does not run it. The `run()` method is optional, and a task that omits it only reports a state. See
[the introduction](index.md) for the full explanation.

## A task reads the config of its workflow

Winslow builds each task with the workflow config of the run. A task reads a value from
`self.workflow_config`, and reads the environment name from `self.env`.

```python
class WriteReport(Task):
    def run(self):
        rows = self.fetch(limit=self.workflow_config.limit)
        self.write(rows, region=self.workflow_config.region)
```

The values thus change between two runs of the same workflow. A task can read them in any method, which
includes the gates below.

## The order of the methods

Winslow calls the methods of a task in this order:

1. `is_eligible()`: is this task relevant for the current config?
2. `can_check()`: may Winslow check the task now?
3. `check()`: is the work already done? If it is, the task is complete and `run()` does not happen.
4. `can_run()`: may the task run now?
5. `run()`: do the work.
6. `can_check()` and `check()` again: confirm the work.

Steps 1, 2 and 4 are gates, and each one returns true by default. A simple task implements only `check()` and
`run()`.

Winslow runs a task only after the tasks it depends on. See [Dependencies](dependencies.md) for the order
between tasks.

## Control the outcome

Three gates decide what happens to a task. Each gate has a method that you override:

| Gate | The method | The question |
| --- | --- | --- |
| Eligibility | `is_eligible()` | Is this task relevant for the current config? |
| Checkability | `can_check()` | May Winslow check the task now? |
| Runnability | `can_run()` | May the task run now? |

A gate method returns true or false. It returns true by default:

```python
class PublishWheel(Task):
    def is_eligible(self):
        return self.env == "prod"
```

### Signal a decision with a reason

A gate returns true or false. A signal method instead attaches a reason to a stop, and records the message on
the task log:

```python
class Publish(Task):
    def can_run(self):
        self.block_if(self.workflow_config.maintenance, "a maintenance window is open")
        return True
```

Each method has its own signal:

| The method | The signal | The status |
| --- | --- | --- |
| `is_eligible()` | `self.skip(msg)` | `SKIPPED` |
| `can_run()` | `self.block(msg)` | `BLOCKED` |
| `can_check()` | `self.block(msg)` | `BLOCKED` |
| `check()` | `self.fail(msg)` | `FAILED` |
| `check()` | `self.require_action(msg)` | `ACTION REQUIRED` |

!!! warning "A gate must return true when no signal fires"

    A signal method raises. The `block_if` form raises only when its condition is true, so a gate that uses it
    falls through to the `return True`. A gate that returns nothing stops the task. Keep the `return True`
    after a conditional signal.

### The signal forms

The `skip`, `block` and `fail` signals have an `_if` form: `skip_if`, `block_if` and `fail_if`. It signals
only when the condition is true. The plain form signals every time, so call it inside your own logic:

```python
class NightlyReport(Task):
    def is_eligible(self):
        if self.env not in ("staging", "prod"):
            self.skip("this task runs in staging and production only")
        return True
```

!!! warning "A signal belongs to one method"

    A signal that a method does not accept becomes an `ERROR`. The `skip` signal is for `is_eligible`, the
    `block` signal is for `can_run` and `can_check`, and the `fail` signal is for `check`. A `skip` inside
    `can_run`, or a `fail` inside `run`, thus errors the task.

## Share a value between check and run

A `transient_property` computes a value once, and caches it for one execution of the task. The first `check`
and the `run` after it share the cache. The final `check` starts with an empty cache:

```python
from winslow import Task, transient_property


class SyncCatalog(Task):
    @transient_property
    def remote_rows(self):
        return fetch_remote_rows()  # An expensive call.

    def run(self):
        write_rows(self.remote_rows)

    def check(self):
        return count_local_rows() == len(self.remote_rows)
```

The first `check` computes `remote_rows`, and `run` reads the same value. A second computation could disagree
with the check. The final `check` computes the value again, so the confirmation reads the state that the run
produced.

For a value that outlives one execution pass - shared reference data, an expensive lookup that many tasks
read - declare a [cache](caching.md) instead.

## A complete example

The `release` workflow shows every gate in one flow. It builds an artifact, tests it, verifies the report, and
publishes it. Each gate reacts to the environment or to a config flag.

```python title="examples/release/workflow.py"
--8<-- "examples/release/workflow.py"
```

[Download this example](https://github.com/winslow-workflow/winslow/blob/main/examples/release/workflow.py)

Run it headless with `winslow run --mode headless --workflow release`. Each variation shows one gate:

| The variation | The result |
| --- | --- |
| The default, in the `dev` environment | Build, test and verify complete. Publish is skipped, because the environment is not production. |
| `WINSLOW_ENV=prod` | Every task completes. |
| `WINSLOW_ENV=prod` and `--maintenance` | Publish is blocked, because a maintenance window is open. |
| `WINSLOW_ENV=prod` and `--offline` | Verify is blocked, and publish waits on it. |

Each task writes a marker into the `state` directory. Delete the directory to run the flow again.
