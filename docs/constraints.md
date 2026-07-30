# Constraints

A constraint is a reusable rule that a task declares in a list. The
[gate methods](tasks.md#control-the-outcome), the success check, and `should_be_initialized` each take such
a list. A constraint packages one rule as a class, so many tasks share it.

## Declare a constraint

A constraint subclasses `Constraint` and implements `apply`. A task declares it in one of its constraint
lists:

```python
from winslow import Constraint, ConstraintType, Task


class ProdOnly(Constraint):
    valid_types = ConstraintType.ELIGIBILITY

    def apply(self, task):
        return self.env == "prod"


class Publish(Task):
    eligibility_constraints = [ProdOnly]
```

Winslow initializes the constraint with the workflow config. Inside `apply`, `self.workflow_config` holds the
config, and `self.env` holds the environment. The `task` argument is the live task instance.

!!! warning "Declare the class, and not an instance"

    Write `[ProdOnly]`, and not `[ProdOnly()]`. Winslow initializes the constraint. A construction with no
    arguments stops with an error that names the mistake.

## The constraint lists

| The list | The method | A failure gives |
| --- | --- | --- |
| `eligibility_constraints` | `is_eligible()` | `SKIPPED` |
| `runnability_constraints` | `can_run()` | `BLOCKED` |
| `checkability_constraints` | `can_check()` | `BLOCKED` |
| `success_constraints` | `check()` | `FAILED` |
| `initialization_constraints` | `should_be_initialized()` | No instance. See below. |

The constraints and the method both run. The constraints run first, so a cheap declarative rule stops the
evaluation before the custom logic. The result is true only when every constraint passes and the method
returns true. A single constraint needs no list: `checkability_constraints = StoreOnline` is legal.

## Limit where a constraint can go

The `valid_types` attribute declares the lists that can hold the constraint. Declare one type, or a
collection of types. The empty default sets no limit. A constraint in a list outside its `valid_types` stops
the task with an error:

```text
Wrong: 'ProdOnly' is not valid as a RUNNABILITY constraint (valid_types=[ELIGIBILITY]).
```

Declare `valid_types` when the rule belongs to one method. A rule that reads an external state, for
example a feature flag, can stay unrestricted and serve more than one list.

## Define success with constraints

A task must define success: override `check`, or declare `success_constraints`. A task with the constraints
alone is complete:

```python
class MarkerWritten(Constraint):
    valid_types = ConstraintType.SUCCESS

    def apply(self, task):
        return marker_exists(task)


class Deploy(Task):
    success_constraints = [MarkerWritten]

    def run(self):
        deploy_and_write_marker(self)
```

The task is `COMPLETED` when the constraint passes after the run. A task with no `check` override and no
success constraints stops with an error, because such a task cannot define success.

A failing success constraint also overrides a passing `check`. The run has already happened. Only the
verdict fails, and the task is `FAILED`.

## Drop a class before creation

The `initialization_constraints` list holds `ClassConstraint` subclasses. The method
[should_be_initialized](parameterization.md#drop-an-instance-before-creation) runs at graph init, before an
instance exists. The constraint is therefore called with the class and the parameters:

```python
from winslow import ClassConstraint, ConstraintType


class EnvAllowed(ClassConstraint):
    valid_types = ConstraintType.INITIALIZATION

    def apply(self, task_class, parameters=None):
        return self.env in task_class.allowed_envs
```

A dropped class produces no instance, and the UI does not show it. The log records each drop with a
"Skipping initialization" line.

## Select the constraints at runtime

Each list has a `get_*_constraints` method, for example `get_eligibility_constraints`. The default returns
the class attribute. Override the method to select the constraints by config or by environment:

```python
class Export(Task):
    def get_eligibility_constraints(self):
        if self.workflow_config.strict:
            return [ProdOnly]
        return None
```

The `get_initialization_constraints` method is a classmethod, because no instance exists at graph init. It
receives the workflow config as its argument.

## A complete example

The `backup` workflow demonstrates every type of constraint. The snapshot defines success with a constraint
alone, the upload declares two runnability rules, one unrestricted rule serves two lists, and the two
production tasks show the difference between a skip and a drop.

```python title="examples/backup/workflow.py"
--8<-- "examples/backup/workflow.py"
```

[Download this example](https://github.com/winslow-workflow/winslow/blob/main/examples/backup/workflow.py)

Run it headless with `winslow run --mode headless --workflow backup`. Each variation shows one list:

| The variation | The result |
| --- | --- |
| The default, in the `dev` environment | Snapshot, upload and verify complete. Replicate is skipped, and prune does not exist. |
| `--maintenance` | Upload is blocked by its runnability constraint, and verify is blocked behind it. |
| `--offline` | The shared store rule blocks the upload run, and verify is blocked behind it. Snapshot completes. |
| `WINSLOW_ENV=prod` | Every task completes, and prune appears in the graph. |

Each task writes a marker into the `state` directory. Delete the directory to run the flow again.
