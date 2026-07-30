# Plugins

A plugin extends Winslow without a fork. There are two kinds:

- A [UI plugin](ui-plugins.md) adds a tab to the terminal UI, or replaces a built-in pane.
- A [filter plugin](filter-plugins.md) adds a command to the filter language.

## How Winslow finds a plugin

A plugin ships as an installed package with an entry point. The group `winslow.tui_plugins` marks a
package with UI plugins, and the group `winslow.filter_plugins` marks a package with filters. One package
can declare both:

```toml title="pyproject.toml of the plugin package"
[project.entry-points."winslow.tui_plugins"]
my-plugin = "my_package"

[project.entry-points."winslow.filter_plugins"]
my-plugin = "my_package"
```

At startup Winslow reads the two groups, imports each package that declares one, and registers every
plugin class that it finds. This happens before the first prompt.

!!! warning "Autodiscovery is opt-out, and not opt-in"

    An installed plugin is code that runs in your process at startup. Treat it with the same care
    as any other dependency. Constrain the discovery in the `pyproject.toml` of the project:

    ```toml
    [tool.winslow]
    disabled_tui_plugins = ["some-plugin"]        # or enabled_tui_plugins to allowlist
    disabled_filter_plugins = ["some-filter"]     # or enabled_filter_plugins to allowlist
    ```

    An entry names the package, or one plugin as `<package>.<plugin-name>`. A plugin class with
    `autoload = False` inverts the rule for itself: it loads only when the enabled list names it.

## The name of a plugin

Every plugin has a name, and the enabled and disabled lists refer to it:

- The default name is the kebab-cased class name: `TeamLinksPlugin` becomes `team-links-plugin`.
- An explicit `name` attribute on the class overrides the default.
- A filter takes its long command as its name, then its short command, and then the kebab-cased
  class name.

The qualified form is `<package>.<name>`, with the distribution name of the package as the prefix.
A built-in plugin has the prefix `builtin`. The qualified name appears in the enabled and disabled
lists, and it is the target format of `replace` (see
[UI plugins](ui-plugins.md#replace-a-built-in-plugin)).

## When a plugin does not appear

- The package must be installed in the same environment as `winslow`.
- The entry point group must be exact: `winslow.tui_plugins` or `winslow.filter_plugins`.
- A UI plugin registers only when its class declares a `slot`.
- A filter without a command is not reachable from an expression. Declare a `long_command`.
- A class with `autoload = False` needs an entry in the enabled list.
