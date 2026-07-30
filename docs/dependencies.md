# Dependencies

Winslow orders the tasks of a workflow with a dependency graph. This page explains how to declare a
dependency, which form to use, and how premier tasks, terminal tasks and groups shape the order.

## Declare dependencies

The `dependencies` attribute holds the tasks that must complete first. It takes a task class, a string, or a
tuple of both:

```python
class TransformData(Task):
    dependencies = DownloadData            # A task class.


class Publish(Task):
    dependencies = (BuildWheel, "test")    # A class and a string.
```

A string resolves to one or more tasks, and Winslow combines every match:

| The string | The match |
| --- | --- |
| `"extract-orders"` | The task with this name. The name is the kebab-cased class name. |
| `"ExtractOrders"` | The task with this class name. |
| `"static"` | Every task in this group. |
| `"self"` | The same task class. |

!!! warning "One string can match more than one task"

    A group name and a task name live in the same lookup. A string that matches a group brings in every task
    of that group. A string that matches nothing raises a `MisconfigurationError`, which makes a stale
    dependency visible at startup.

Winslow builds the graph from these declarations, and it rejects a cyclic reference with an error.

### Which form to use

| The form | Use it for |
| --- | --- |
| A task class | A fixed dependency inside one workflow. The interpreter resolves the name, so a rename or a deletion fails at import. This strict form suits a tightly coupled flow. |
| A task name | A dependency you do not want to import, or one that another team owns. Pin the name with `name = "..."`, and the dependency survives a rename of the class. A wrong name fails at startup, not at import. |
| A group name | A dependency on a category, not on one task. A new task in the group becomes a dependency, and this declaration does not change. |

!!! tip "A group dependency survives a skipped task"

    Winslow keeps a dependency only on an eligible task. It drops an ineligible task from the dependency set.
    A task that depends directly on one task that becomes ineligible thus loses the dependency, and it can run
    earlier than intended. A group dependency keeps every eligible task of the group, so the order to those
    tasks holds.

### Control a single dependency

The `dependencies` attribute works on the classes. The `depends_on` method works on the instances. Winslow
calls it for each candidate pair, and the pair becomes a dependency only when the method returns true:

```python
class Deploy(Task):
    dependencies = Build

    def depends_on(self, task):
        return task.region == self.region
```

Use this method when the dependency holds only for some pairs of instances.

## Premier and terminal tasks

A premier task runs before every other task. Every task that is not premier depends on every premier
task, and no declaration is necessary:

```python
class Authenticate(Task):
    is_premier = True
```

A terminal task runs at the end, for example a task that sends a notification:

```python
class NotifyTeam(Task):
    is_terminal = True
```

Winslow rejects two declarations with a `MisconfigurationError`:

- A premier task cannot depend on a task that is not premier.
- A task that is not terminal cannot depend on a terminal task.

## Groups

A group is a label. The `groups` attribute takes one name or a tuple of names:

```python
class Lint(Task):
    groups = "static"


class UnitTest(Task):
    groups = ("tests", "fast")
```

A group has two uses. It is a dependency target, as the table above shows. It is also a filter target, so
`--filter '!g static'` selects every task of the group. See [Filters](filters.md).

## A complete example

The `delivery` workflow uses every dependency form in one graph. An ingest group feeds a validate task, a
reconcile task and a transform task. A deploy task depends on the transform, the reconcile and the critical
checks.

```python title="examples/delivery/workflow.py"
--8<-- "examples/delivery/workflow.py"
```

[Download this example](https://github.com/winslow-workflow/winslow/blob/main/examples/delivery/workflow.py)

The declarations resolve to these dependencies:

| The task | Its form | It depends on |
| --- | --- | --- |
| `validate` | A group name, `"ingest"` | `ingest-orders`, `ingest-payments` |
| `reconcile` | A task name, `"ingest-orders"` | `ingest-orders` |
| `transform` | A task class, `Validate` | `validate` |
| `deploy` | Two classes and a group, with `depends_on` | `transform`, `reconcile`, `security-scan` |

The group name `"ingest"` and the task name `"ingest-orders"` show the difference between the two. The group
brings in every ingest task. The name brings in one.

Two tasks are left out of a dependency:

- `ingest-legacy` is not eligible by default, so Winslow drops it from the ingest group. Pass `--legacy` to
  make it eligible, and validate then depends on it too.
- `style-check` is not critical, so the `depends_on` method of deploy drops it. Of the checks group, deploy
  depends on `security-scan` only.

Run it headless with `winslow run --mode headless --workflow delivery`.
