import collections
from types import SimpleNamespace
import networkx as nx

from winslow._base import _Base
from winslow.logger import LOGGER
from winslow.cache import CacheContainerRef, get_global_cache, get_workflow_cache
from winslow.task import Task
from winslow.task.eligibility import check_task_eligibility

from winslow._parameterization import _get_parameterization_context
from winslow.exceptions import (
    MisconfigurationError,
    TaskSkip,
    CyclicalDependencyError,
)


class Graph(_Base):
    """
    A graph does three things:

    1. It initializes the tasks.
    2. It assigns the dependencies and finds a cyclical reference.
    3. It builds the pipeline in the correct run order.
    """

    # generate_pipeline runs inside the cache context of the workflow, on the
    # initializing thread, so the fallbacks of these descriptors resolve there.
    workflow_cache = CacheContainerRef("_workflow_cache_container", get_workflow_cache)
    global_cache = CacheContainerRef("_global_cache_container", get_global_cache)

    def __init__(self, orchestrator_config, workflow_config):
        super().__init__(orchestrator_config)

        self.workflow_config = workflow_config

        # Nothing stamps a graph, so the descriptors always use the fallbacks.
        self._workflow_cache_container = None
        self._global_cache_container = None

        # A parameterized task class can have more than one instance.
        # key: task_kls, value: one or more task objects
        self._task_class_map = collections.defaultdict(set)

    def _get_nx_graph(self, tasks_with_assigned_deps):
        nx_graph = nx.DiGraph()

        # add_nodes_from: a task with no dependency and no dependent contributes
        # no edge, but it still needs a priority stamp.
        nx_graph.add_nodes_from(tasks_with_assigned_deps)
        for task in tasks_with_assigned_deps:
            for dep in task.dependent_tasks:
                nx_graph.add_edge(task, dep)
        return nx_graph

    def _check_cyclical_dependencies(self, nx_graph):
        for cycle in nx.simple_cycles(nx_graph):
            raise CyclicalDependencyError(cycle)

    def _assign_task_priorities(self, nx_graph):
        """The priority of a task is its topological generation, which sets the
        execution order. An edge points from a task to a dependency, so the
        generations are calculated on the reverse view."""
        generations = nx.topological_generations(nx_graph.reverse(copy=False))
        for priority, generation in enumerate(generations):
            for task in generation:
                task._priority = priority

    def _check_premier_dependencies(self, task, deps):
        if not task.is_premier:
            return
        for dep in deps:
            if not dep.is_premier:
                raise MisconfigurationError(
                    f"Premier task {task} cannot depend on non-premier task {dep}."
                )

    def _check_terminal_dependencies(self, task, deps):
        if task.is_terminal:
            return
        for dep in deps:
            if dep.is_terminal:
                raise MisconfigurationError(
                    f"Non-terminal task {task} cannot depend on terminal task {dep}."
                )

    def _assign_task_dependencies(self, task, registry):
        """
        Read the dependency context that the class-level attribute declares. It
        can hold strings, such as a task name or a group name, and also task
        class objects.

        For each string entry, get the task class objects from the registry.
        """

        dependency_classes = []

        ctx = task._get_dependency_context()

        for dep in ctx:
            # Examples of a string dependency:
            # self (a dependency on the same task class)
            # my-task-name
            # my-task-group
            # MyTaskClass (the class name as a string)

            if isinstance(dep, str):
                if dep.lower() == "self":
                    task_classes = [task.__class__]
                else:
                    task_classes = registry.filter_by_string(dep)

                # A dependency class that is not initialized is correct. But the
                # registry must have an entry for the class. If it does not, the
                # declaration is wrong.

                # This occurs more frequently with the 'name' attribute, or with
                # a class name in string form.
                if not task_classes:
                    raise MisconfigurationError(
                        f"No matching task classes found in the registry for the dependency: {dep}",
                        "Make sure you don't have stale dependencies set in string form.",
                    )
            elif isinstance(dep, type) and issubclass(dep, Task):
                task_classes = [dep]
            else:
                raise MisconfigurationError(
                    f"Invalid dependency value ({dep}, {type(dep)}), it can either be a task class or a string."
                )

            dependency_classes.extend(task_classes)

        # A task that is not premier depends on each premier task implicitly.
        if not task.is_premier:
            dependency_classes = set(dependency_classes).union(
                registry.premier_task_classes
            )

        # A group name and an explicit task of the same group can both be
        # dependency entries. The matches are then duplicates.

        # TODO: improve the performance. Collect all dependency candidates and
        # check their eligibility in parallel.
        deps_unique = {
            dep_task
            for task_kls in dependency_classes
            for dep_task in self._task_class_map[task_kls]
            if task.depends_on(dep_task) and check_task_eligibility(dep_task)
        }

        self._check_premier_dependencies(task, deps_unique)
        self._check_terminal_dependencies(task, deps_unique)

        task._dependent_tasks = tuple(deps_unique)

    def _pipeline_sort_func(self, task):
        return (
            *task.execution_order,
            tuple(sorted(task.get_groups())),
            task.instance_name,
            str(task),
        )

    def generate_pipeline(self, registry):
        tasks = self._initialize_tasks(registry)

        for task in tasks:
            self._task_class_map[task.__class__].add(task)

        for task in tasks:
            self._assign_task_dependencies(task, registry)

        nx_graph = self._get_nx_graph(tasks)
        self._check_cyclical_dependencies(nx_graph)
        self._assign_task_priorities(nx_graph)

        for task in tasks:
            task._dependent_tasks = tuple(
                sorted(task._dependent_tasks, key=self._pipeline_sort_func)
            )

        result = []

        for idx, task in enumerate(
            sorted(
                tasks,
                key=self._pipeline_sort_func,
            )
        ):
            task._index = idx
            result.append(task)

        return tuple(result)

    def _initialize_tasks(self, registry):
        result = []

        for task_kls in registry.classes:
            if task_kls._is_parameterized:
                for params in _get_parameterization_context(
                    task_kls=task_kls, workflow_config=self.workflow_config
                ):
                    params = SimpleNamespace(**params)

                    if self._should_initialize_task(task_kls, parameters=params):
                        LOGGER.debug(
                            f"Initializing {task_kls} with parameters {params}"
                        )
                        task_obj = self.initialize_task(task_kls, parameters=params)
                        result.append(task_obj)

            elif self._should_initialize_task(task_kls):
                result.append(self.initialize_task(task_kls))

        if self.orchestrator_config.is_interactive:
            for task in result:
                task._enable_log_buffer()

        return result

    def _should_initialize_task(self, task_kls, parameters=None):
        try:
            result = task_kls._evaluate_should_be_initialized(
                workflow_config=self.workflow_config, parameters=parameters
            )
        except TaskSkip:
            result = False

        if not result:
            LOGGER.info(
                f"Skipping initialization: {task_kls}. Config: {self.workflow_config}. Parameters: {parameters}"
            )
        return result

    def initialize_task(self, task_kls, parameters=None):
        """Override this to control how a task is initialized."""
        return task_kls(workflow_config=self.workflow_config, parameters=parameters)
