import os
import sys

from winslow.registry import Registry
from winslow.util import classes_in_module, iter_dir_modules
from winslow.cache.base import GlobalCache, WorkflowCache, validate_cache_class


class _CacheRegistry(Registry):
    """A cache class lives in a cache.py file or a cache/ package. The registries
    import only these locations, so startup stays cheap (compare telemetry.py)."""

    file_filter = {"cache.py"}
    dir_filter = {"cache"}

    def register(self, klass):
        # A bad declaration fails at collection, not at first use.
        validate_cache_class(klass)
        super().register(klass)


class GlobalCacheRegistry(_CacheRegistry):
    """Collects the GlobalCache classes of a project, from the whole tree: a
    workflow directory can also declare a global cache."""

    item_class = GlobalCache


class WorkflowCacheRegistry(_CacheRegistry):
    """Collects the WorkflowCache classes of one workflow, from its module
    directory."""

    item_class = WorkflowCache


def stray_workflow_caches(directory, workflow_directories):
    """The concrete WorkflowCache classes that no workflow directory contains.
    No workflow collects such a class, so the caller warns about each one."""
    roots = tuple(os.path.abspath(wd) + os.sep for wd in workflow_directories)
    return [
        kls
        for module in iter_dir_modules(directory, only={"cache.py"}, under={"cache"})
        for kls in classes_in_module(module, WorkflowCache)
        if not _source_path(kls).startswith(roots)
    ]


def _source_path(kls):
    return os.path.abspath(sys.modules[kls.__module__].__file__)
