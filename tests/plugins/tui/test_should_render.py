"""The should_render hook: a plugin can keep itself out of one composition."""

from types import SimpleNamespace

from winslow.ui.plugin import UIPlugin
from winslow.ui.builtin_plugins.workflow.caches import CachesPanePlugin
from winslow.ui.builtin_plugins.workflow.cache_overview import CacheOverviewPlugin


def _context(workflow_caches, global_caches):
    workflow = SimpleNamespace(
        workflow_cache=SimpleNamespace(caches=lambda: workflow_caches),
        global_cache=SimpleNamespace(caches=lambda: global_caches),
    )
    return SimpleNamespace(workflow=workflow)


def test_should_render_defaults_to_true():
    assert UIPlugin.should_render(context=None) is True


def test_cache_plugins_hide_without_registered_caches():
    context = _context((), ())
    assert CachesPanePlugin.should_render(context) is False
    assert CacheOverviewPlugin.should_render(context) is False


def test_cache_plugins_render_with_a_cache_in_either_scope():
    assert CachesPanePlugin.should_render(_context((), ("global",))) is True
    assert CacheOverviewPlugin.should_render(_context(("session",), ())) is True


def test_companion_resolves_masters_to_their_detail(make_registry):
    from winslow.ui.builtin_plugins.workflow.cache_overview import CacheOverviewPlugin
    from winslow.ui.builtin_plugins.workflow.caches import CachesPanePlugin
    from winslow.ui.builtin_plugins.workflow.history import HistoryPlugin
    from winslow.ui.builtin_plugins.workflow.task_overview import TaskOverviewPlugin
    from winslow.ui.builtin_plugins.workflow.tasks_pane import TasksPanePlugin

    import winslow.ui.builtin_plugins.workflow as builtin

    registry = make_registry()
    registry.discover(builtin)

    assert registry.companion(TasksPanePlugin) is TaskOverviewPlugin
    assert registry.companion(HistoryPlugin) is TaskOverviewPlugin
    assert registry.companion(CachesPanePlugin) is CacheOverviewPlugin
    # A detail tab has no companion of its own: the rule cannot loop.
    assert registry.companion(TaskOverviewPlugin) is None

    # A replacement subclass keeps the pairing of its target.
    class Replacement(TasksPanePlugin):
        pass

    assert registry.companion(Replacement) is TaskOverviewPlugin


def test_any_tabbed_respects_should_render(make_registry):
    import winslow.ui.builtin_plugins.workflow as builtin

    from winslow.ui.plugin import Slots

    registry = make_registry()
    registry.discover(builtin)

    with_caches = _context(("session",), ())
    without_caches = _context((), ())
    assert registry.any_tabbed(with_caches, Slots.TASK_OVERVIEW) is True
    assert registry.any_tabbed(without_caches, Slots.TASK_OVERVIEW) is False
