import weakref

from winslow.exceptions import IdentityKeyCollisionError


class TaskIndex:
    """Map each identity key to its live task, for one session. The references
    are weak: the store owns the tasks, and the index only resolves keys."""

    def __init__(self, tasks=None):
        self._tasks = weakref.WeakValueDictionary()
        for task in tasks or ():
            self.add(task)

    def add(self, task):
        existing = self._tasks.get(task.identity_key)
        if existing is not None and existing is not task:
            raise IdentityKeyCollisionError(
                f"identity key {task.identity_key!r} resolves to two live tasks: "
                f"{existing} and {task}. Their parameter reprs are equal - give "
                f"each parameter value a distinct and stable repr."
            )
        self._tasks[task.identity_key] = task

    def resolve(self, key):
        task = self._tasks.get(key)
        if task is None:
            raise KeyError(
                f"identity key {key!r} does not resolve to a live task - the "
                f"session released its tasks, or the key belongs to another session."
            )
        return task

    def get(self, key):
        return self._tasks.get(key)

    def __len__(self):
        return len(self._tasks)
