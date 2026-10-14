# winslow-sample-filter-plugin

One filter command for Winslow. Install the package where the engine runs, `winslow run` or
`winslow serve`, then list the class in the settings module of the project (see `docs/settings.md`):

```python
FILTER_PLUGINS = ["winslow_sample_filter_plugin.filters.DependsOnFilter"]
```
