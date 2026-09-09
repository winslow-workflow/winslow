# UI plugins

A UI plugin extends the terminal UI without a fork. The UI is a set of named slots, and every pane in it is
a plugin that fills one slot. The built-in panes use the same mechanism, so a third-party plugin can sit
next to them, come before them, or replace one of them.

A UI plugin ships in a plugin package with an entry point in the `winslow.tui_plugins` group (see
[How Winslow finds a plugin](plugins.md#how-winslow-finds-a-plugin)).

## Write a UI plugin

A plugin declares a slot and a label, and builds one widget:

```python title="examples/winslow-sample-tui-plugin/winslow_sample_tui_plugin/dashboard.py"
--8<-- "examples/winslow-sample-tui-plugin/winslow_sample_tui_plugin/dashboard.py"
```

`create_widget` returns any Textual widget. The `context` argument carries the state of the screen: the
session port client and value shapes (see [the payload rule](#the-payload-rule)):

| The screen | The context | The useful attributes |
| --- | --- | --- |
| Dashboard | `DashboardRenderContext` | `client` (the `AppClient`), `descriptors` |
| Workflow | `WorkflowRenderContext` | `client` (the `SessionClient`), `session`, `snapshot`, `roster`, `task_statuses` |
| Task info modal | `TaskDetailRenderContext` | `info` (a `TaskInfo` value, not the task), `logs`, `client`, `task_key`, `root_dir` |
| Confirmation modal | `WorkflowConfirmationRenderContext` | `workflow` (the workflow name), `form_values` |

The workflow context attributes:

- `client`: the `SessionClient` of the session. It serves every read (`roster()`, `history()`,
  `caches()`, `task_detail(key)`, ...) and accepts every action through `submit(action)`.
- `session`: the `SessionRow` value: display name, instance name, status, the task status summary.
- `snapshot`: the `SessionSnapshot` at compose time: task statuses by identity key, batch rows, the
  session log backlog, the cache names.
- `roster`: one stub `TaskInfo` per task, in launch-filter order.
- `task_statuses`: the `{key: TaskStatus}` mapping that the screen maintains.

A slot with one plugin shows the widget directly. A slot with two or more plugins becomes a tab bar, and
`label` names each tab. When one slot of a row becomes tabbed, the other slots of that row become tabbed
too, so the row keeps one visual line.

## The payload rule

A render context, a Textual message and a session bus event carry values: the identity key of the task
(`Task.identity_key`, a stable string), `TaskInfo` captures, and the model dataclasses of
`winslow.model`. A pane reads through the context `client` and never holds a live core object. A pane
built this way works the same on a remote client, because every payload can cross a process boundary.

The task events of the workflow screen:

| The event | The payload |
| --- | --- |
| `TaskStatusChanged` | `key`, `status` |
| `ExecutionStatusChanged` | `batch_uuid`, `task_key`, `status` |
| `TaskLogUpdated` | `batch_uuid`, `task_key`, `line` |
| `BatchCreated`, `BatchCompleted` | `info` (a `BatchInfo` value) |

A pane keys its rows by the identity key, and it reads the current statuses from
`WorkflowRenderContext.task_statuses`, the `{key: TaskStatus}` mapping that the screen maintains:

```python
class StatusBoardPlugin(UIPlugin):
    slot = Slots.TASKS_PANE
    label = "Status Board"

    def create_widget(self, context):
        return StatusBoard(statuses=context.task_statuses)


class StatusBoard(Widget):
    @on(TaskStatusChanged)
    def refresh_row(self, event):
        self.rows[event.key].status = event.status
```

A pane that needs more than its messages carry reads through the client, for example
`context.client.task_detail(key)` for the full capture of one task, or
`context.client.submit(RunTasks(keys=(key,)))` for an action. Every client method takes values and
returns values, so the same pane renders a local session and a remote one.

Two panes are local by nature and stay outside the port: the system resources pane describes the
machine the widget runs on, and the dashboard log pane shows the log of the process the TUI runs in.

## The slots

Each screen declares its slots on the `Slots` class. Press `ctrl+g` on a screen and Winslow covers every
slot with its name, so you can see what each slot spans:

![The dashboard, with each slot highlighted by the slot inspector](images/dashboard-slots.svg)

The dashboard slots and the built-in plugins in them:

| The slot | The built-in content |
| --- | --- |
| `DASHBOARD_WORKFLOWS` | The workflow selector. |
| `DASHBOARD_WORKFLOW_FORM` | The workflow form. |
| `DASHBOARD_SESSIONS` | The session list, with a History tab. |
| `DASHBOARD_LOGS` | The application logs. |
| `DASHBOARD_RESOURCES` | The system resources. |

![The workflow screen, with each slot highlighted by the slot inspector](images/workflow-slots.svg)

The workflow screen slots:

| The slot | The built-in content |
| --- | --- |
| `TASKS_PANE` | The task list, with a History tab. |
| `TASK_OVERVIEW` | The overview of the selected task. |
| `WORKFLOW_LOGS` | The session logs. |
| `WORKFLOW_RESOURCES` | The system resources. |

Two slots live in modals, and not on a screen: `TASK_DETAIL` fills the task info modal, and
`WORKFLOW_CONFIRMATION` fills the confirmation modal before a workflow starts.

## Order the plugins in a slot

The `priority` value orders the plugins of one slot. A lower value comes first, and the first plugin is the
first tab. Each built-in plugin starts at 5, and the next one in the same slot is one higher. The values 0
to 4 are reserved for a plugin that must come before the built-in plugins:

```python
class SampleDashboardPlugin(UIPlugin):
    slot = Slots.DASHBOARD_WORKFLOWS
    label = "Sample"
    priority = 6      # After the built-in workflow selector.
```

Two plugins with the same priority order by their plugin name. Declare a priority instead of depending on
that order.

## Replace a built-in plugin

The `replace` argument evicts another plugin and takes its place. The target is the qualified plugin name
(see [The name of a plugin](plugins.md#the-name-of-a-plugin)). A built-in plugin has the prefix `builtin`.
This example puts a mood face on the system resources pane, and keeps the built-in stat widgets:

```python title="examples/winslow-sample-tui-plugin/winslow_sample_tui_plugin/mood.py"
--8<-- "examples/winslow-sample-tui-plugin/winslow_sample_tui_plugin/mood.py"
```

The plugin subclasses the built-in plugin, so it inherits the slot of its target. A replacement that
declares its own slot must declare the same slot as its target. A different slot is an error, because a
replacement changes the content of a pane and not the layout of the screen.

## A complete example

The `winslow-sample-tui-plugin` package holds the two plugins of this page:

```
winslow-sample-tui-plugin/
├── pyproject.toml
└── winslow_sample_tui_plugin/
    ├── __init__.py
    ├── dashboard.py     # adds a tab
    └── mood.py          # replaces the system resources pane
```

[Download this example](https://github.com/winslow-workflow/winslow/tree/main/examples/winslow-sample-tui-plugin)

The `pyproject.toml` declares the entry points:

```toml title="examples/winslow-sample-tui-plugin/pyproject.toml"
--8<-- "examples/winslow-sample-tui-plugin/pyproject.toml"
```

Install the package into the workflow project. An editable install keeps a local plugin live while
you work on it:

=== "uv"

    ```bash
    uv add --editable ../winslow-sample-tui-plugin
    ```

    The command adds the dependency and a local source to the `pyproject.toml` of the project:

    ```toml title="pyproject.toml of the workflow project"
    [project]
    dependencies = [
        "winslow-sample-tui-plugin",
    ]

    [tool.uv.sources]
    winslow-sample-tui-plugin = { path = "../winslow-sample-tui-plugin", editable = true }
    ```

=== "pip"

    ```bash
    pip install -e ../winslow-sample-tui-plugin
    ```

A plugin package can also add a command to the filter language (see
[Filter plugins](filter-plugins.md)).
