import logging
from dataclasses import dataclass

from winslow.autodiscovery import Registerable, BaseRegistry
from winslow.exceptions import PluginError
from winslow.util import to_tuple

logger = logging.getLogger(__name__)

from textual.containers import Container
from textual.widgets import TabbedContent, TabPane


@dataclass
class Slot:
    id: str


class RenderContext:
    pass


@dataclass
class WorkflowRenderContext(RenderContext):
    workflow: object

    @property
    def workflow_config(self):
        return self.workflow.workflow_config

    @property
    def orchestrator_config(self):
        return self.workflow.orchestrator_config


@dataclass
class DashboardRenderContext(RenderContext):
    orchestrator: object
    workflow_context: dict

    @property
    def orchestrator_config(self):
        return self.orchestrator.orchestrator_config


@dataclass
class TaskDetailRenderContext(RenderContext):
    # The TaskInfo value, never the task. The rename from `task` breaks an old
    # plugin loudly on the missing attribute.
    info: object
    logs: list | None = None
    # The transient_property snapshots of this task, per phase, in one batch:
    # {ExecutionPhase: {name: value}}. It is set only for a detail view at row
    # level, which comes from the execution history. It is None for the plain
    # task-list view.
    transient_snapshots: dict | None = None
    # The cache reads of this task, per phase, in one batch:
    # {ExecutionPhase: tuple[CacheReadSnapshot]}. Same scoping rules as
    # transient_snapshots.
    cache_snapshots: dict | None = None


@dataclass
class WorkflowConfirmationRenderContext(RenderContext):
    workflow_kls: object
    form_values: object


class Slots:
    # Workflow screen
    TASKS_PANE = Slot("tasks-pane")
    TASK_OVERVIEW = Slot("task-overview")
    WORKFLOW_LOGS = Slot("workflow-logs")
    WORKFLOW_RESOURCES = Slot("workflow-resources")
    TASK_DETAIL = Slot("task-detail")
    # Dashboard screen
    DASHBOARD_WORKFLOWS = Slot("dashboard-workflows")
    DASHBOARD_WORKFLOW_FORM = Slot("dashboard-workflow-form")
    DASHBOARD_SESSIONS = Slot("dashboard-sessions")
    DASHBOARD_LOGS = Slot("dashboard-logs")
    DASHBOARD_RESOURCES = Slot("dashboard-resources")
    WORKFLOW_CONFIRMATION = Slot("workflow-confirmation")


class UIPlugin(Registerable):
    slot = None
    label = ""
    # In a slot, a lower priority comes first. A built-in plugin starts at 5 and
    # the next one is higher. The values 0 to 4 are thus free for a third-party
    # plugin that must come before the built-in plugins.
    priority = 5
    replace = None
    # The master plugins this detail plugin accompanies: when a tab of one of
    # them activates, the screen brings this plugin's tab forward. A single
    # class or a tuple (see UIPluginRegistry.companion).
    detail_of = None

    @classmethod
    def should_render(cls, context):
        """Return False to keep the plugin out of one screen composition, for
        example when the project registers nothing the pane would show."""
        return True

    def create_widget(self, context):
        raise NotImplementedError


class UIPluginRegistry(BaseRegistry):
    _config_key = "tui_plugins"
    _entry_point_group = "winslow.tui_plugins"
    _base_class = UIPlugin

    def __init__(self):
        super().__init__()
        self._plugins = []
        self._sources = {}

    def _qname(self, cls):
        return cls._qualified_name(self._sources.get(cls, "builtin"))

    def _is_candidate(self, cls):
        return cls.slot is not None

    def _register(self, cls, source):
        # A replace plugin with autoload=False would remove its target and put
        # nothing in its place.
        if cls.replace and not cls._should_load(source, self._enabled):
            qname = cls._qualified_name(source)
            raise PluginError(
                f"Plugin {qname} declares replace={cls.replace!r} but has autoload=False "
                f"and is not in enabled_tui_plugins — target would be evicted with nothing replacing it"
            )
        super()._register(cls, source)

    def _do_register(self, cls, source):
        self._sources[cls] = source
        qname = self._qname(cls)

        if cls.replace:
            target = next(
                (p for p in self._plugins if self._qname(p) == cls.replace), None
            )
            if target is None:
                # A miss in an empty slot means that this registry is not scoped
                # for this plugin.
                if any(p.slot == cls.slot for p in self._plugins):
                    logger.warning(
                        f"Plugin {qname} declared replace={cls.replace!r} but no matching plugin was found"
                    )
                return
            elif target.slot != cls.slot:
                raise PluginError(
                    f"Plugin {qname} cannot replace {cls.replace!r}: "
                    f"slot mismatch ({cls.slot.id!r} vs {target.slot.id!r})"
                )
            else:
                self._plugins.remove(target)
                logger.info(f"Plugin {self._qname(target)} replaced by {qname}")

        for existing in self._plugins:
            if self._qname(existing) == qname:
                raise PluginError(f"Plugin name clash: {qname} already registered")

        self._plugins.append(cls)
        self._plugins.sort(key=lambda p: (p.priority, p.get_name()))

    def register(self, cls, source="builtin"):
        """The public API to register a plugin directly."""
        self._register(cls, source)

    def for_slot(self, slot):
        return [p for p in self._plugins if p.slot is not None and p.slot.id == slot.id]

    def companion(self, master_cls):
        """The detail plugin whose detail_of names the master. The match is by
        subclass, so a replacement plugin keeps the pairing of its target."""
        return next(
            (
                plugin
                for plugin in self._plugins
                if plugin.detail_of
                and any(issubclass(master_cls, m) for m in to_tuple(plugin.detail_of))
            ),
            None,
        )

    def rendered_for_slot(self, slot, context):
        """The plugins of the slot that render for this composition."""
        return [p for p in self.for_slot(slot) if p.should_render(context)]

    def compose_slot(self, slot, context, force_tabbed=False):
        plugins = self.rendered_for_slot(slot, context)
        if not plugins:
            return
        content_class = f"{slot.id}-content"
        with Container(classes=slot.id):
            if len(plugins) == 1 and not force_tabbed:
                w = plugins[0]().create_widget(context)
                w.add_class(content_class)
                yield w
            else:
                with TabbedContent():
                    for plugin_cls in plugins:
                        with TabPane(plugin_cls.label) as pane:
                            # The stamp names the pane's plugin, so the screen
                            # can pair master and detail tabs (see companion).
                            pane.w_plugin = plugin_cls
                            w = plugin_cls().create_widget(context)
                            w.add_class(content_class)
                            yield w

    def any_tabbed(self, context, *slots):
        return any(len(self.rendered_for_slot(slot, context)) > 1 for slot in slots)
