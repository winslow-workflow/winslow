# Filter plugins

A filter plugin adds a command to the [filter language](filters.md). The built-in commands select a task by
its name and by its group. A plugin command can select by any property of a task.

A filter ships in a plugin package with an entry point in the `winslow.filter_plugins` group (see
[How Winslow finds a plugin](plugins.md#how-winslow-finds-a-plugin)).

## Write a filter

A filter declares its commands, one match rule, and one explanation:

```python title="examples/winslow-sample-filter-plugin/winslow_sample_filter_plugin/filters.py"
--8<-- "examples/winslow-sample-filter-plugin/winslow_sample_filter_plugin/filters.py"
```

The command `!needs`, or its short form `!n`, then works in every filter expression. The operators of
the language combine a plugin command like any other selection. The task overview shows the
dependencies of a task, so the result of this filter is visible on the screen:

```bash
winslow run --filter '!needs unit-test'   # integration-test
winslow run --filter '!n test'            # integration-test, build-wheel
```

The three parts of the contract:

- The parser passes the text after the command to the filter as `self.value`. The value is always a
  string. A filter that converts it must not raise on a bad value: return false, and let the bad
  value select nothing.
- `matches` runs once per task, and true selects the task.
- `explain` returns the rule as a short sentence, for example `depends on unit-test`. A parsed query
  reports itself with these sentences.

## The command names

Declare a `long_command`, and add a `short_command` when a shorthand helps. Each command must be unique
across every registered filter. A clash stops the startup with an error that names both filters. The
built-in commands are `!g` and `!group`; a bare word needs no command, because it is the name selection.
The commands also give the filter its name (see
[The name of a plugin](plugins.md#the-name-of-a-plugin)).

## A complete example

The `winslow-sample-filter-plugin` package holds the filter of this page:

```
winslow-sample-filter-plugin/
├── pyproject.toml
└── winslow_sample_filter_plugin/
    ├── __init__.py
    └── filters.py       # adds the !needs command
```

[Download this example](https://github.com/winslow-workflow/winslow/tree/main/examples/winslow-sample-filter-plugin)

The `pyproject.toml` declares the entry point:

```toml title="examples/winslow-sample-filter-plugin/pyproject.toml"
--8<-- "examples/winslow-sample-filter-plugin/pyproject.toml"
```

Install the package into the workflow project. An editable install keeps a local plugin live while
you work on it:

=== "uv"

    ```bash
    uv add --editable ../winslow-sample-filter-plugin
    ```

    The command adds the dependency and a local source to the `pyproject.toml` of the project:

    ```toml title="pyproject.toml of the workflow project"
    [project]
    dependencies = [
        "winslow-sample-filter-plugin",
    ]

    [tool.uv.sources]
    winslow-sample-filter-plugin = { path = "../winslow-sample-filter-plugin", editable = true }
    ```

=== "pip"

    ```bash
    pip install -e ../winslow-sample-filter-plugin
    ```
