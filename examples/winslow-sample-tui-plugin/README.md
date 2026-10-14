# winslow-sample-tui-plugin

Two UI plugins for the Winslow terminal: a dashboard pane, and a replacement of the built-in system
resources pane. Install the package where `winslow run` or `winslow connect` runs, then list the classes
in the settings module of the project (see `docs/settings.md`):

```python
TUI_PLUGINS = [
    "winslow_sample_tui_plugin.dashboard.SampleDashboardPlugin",
    "winslow_sample_tui_plugin.mood.ResourcesMoodPlugin",
]
```
