# Security Policy

## Trust model (read this first)

Winslow runs the code in the directory you point it at. Treat a workflow
directory like a `Makefile` or a `conftest.py`: **running `winslow` in a
directory executes that directory's code** — each workflow's `workflow.py` and
the `.py` files beside it, plus modules used for orchestrator discovery, are
imported at startup, before any prompt.

Additionally, plugin and filter autodiscovery is **opt-out**: any installed
distribution exposing `winslow.tui_plugins` / `winslow.filter_plugins` entry
points is imported and its `autoload = True` classes registered on startup.
Constrain this from your `pyproject.toml`:

```toml
[tool.winslow]
disabled_tui_plugins = ["some-plugin"]        # or enabled_tui_plugins to allowlist
disabled_filter_plugins = ["some-filter"]     # likewise enabled_filter_plugins
```

Only run winslow in project directories and with dependencies you trust. This is
by design and standard for the category (make, tox, pytest) — it is not itself a
vulnerability.

## Reporting a vulnerability

If you find a security issue that goes beyond the trust model above (e.g. code
execution from data that shouldn't be trusted, path traversal, a way to bypass
the plugin allow/deny lists), please report it privately:

- Use GitHub's **private vulnerability reporting** (Security tab → "Report a
  vulnerability") on the repository, or
- Email **ocanbascil@gmail.com** with a description and reproduction.

Please don't open a public issue for a suspected vulnerability until it's been
triaged. Reports get acknowledged, investigated, and coordinated toward a fix
and disclosure.

## Supported versions

Winslow is pre-1.0; fixes land on the latest `0.x` release.
