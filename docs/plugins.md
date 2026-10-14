# Plugins

A plugin extends Winslow without a fork. There are two kinds:

- A [UI plugin](ui-plugins.md) adds a tab to the terminal UI, or replaces a built-in pane.
- A [filter plugin](filter-plugins.md) adds a command to the filter language.

## How Winslow finds a plugin

A plugin is a class the [settings module](settings.md) names. `TUI_PLUGINS` lists the UI plugins,
`FILTER_PLUGINS` the filters, each as the dotted path of the class:

```python title="etl/settings.py"
TUI_PLUGINS = ["winslow_sample_tui_plugin.dashboard.SampleDashboardPlugin"]
FILTER_PLUGINS = ["winslow_sample_filter_plugin.filters.DependsOnFilter"]
```

At startup Winslow imports each listed class and registers it beside the built-ins, in list order.
The lists are the whole set: Winslow loads the listed classes and the built-ins.

## Where a plugin runs

Under `winslow run` everything runs in one process, and one install covers both kinds. Under
`winslow serve` the two kinds live on different sides:

- A UI plugin runs in the terminal. Install it where `winslow connect` runs, and every terminal that
  wants the tab installs it.
- A filter plugin runs in the engine. The server parses every filter expression, from a terminal or an
  agent, so install it where `winslow serve` runs.

## The name of a plugin

Every plugin has a name:

- The default name is the kebab-cased class name: `TeamLinksPlugin` becomes `team-links-plugin`.
- An explicit `name` attribute on the class overrides the default.
- A filter takes its long command as its name, then its short command, and then the kebab-cased
  class name.

Every plugin of a screen has its own name, and every filter its own command. The name is the target
of `replace` (see [UI plugins](ui-plugins.md#replace-a-built-in-plugin)).

## When a plugin does not appear

- The package must be installed in the same environment as `winslow`, and the class listed in the
  settings module of that side (see above).
- A UI plugin registers only when its class declares a `slot`.
- A filter without a command is not reachable from an expression. Declare a `long_command`.
