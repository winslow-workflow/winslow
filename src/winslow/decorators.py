from winslow.util import safe_repr
from winslow.cache import phase_cache


class TransientProperty:
    """Like @cached_property, but the scope is one batch execution. The key
    is the batch_uuid, so two concurrent batches on the same task object do
    not interfere.

    The first access in a pass sets the value: __get__ calls fget(task) and
    stores the result in the phase cache of the batch. Later accesses in
    the same pass read the cache. The runner clears the cache at each
    checkability gate (see ExecutionPhase.resets_cache), which opens a new
    pass. Access outside a batch execution context raises RuntimeError.

    Example:

        class Publish(Task):
            @transient_property
            def remote_etag(self):
                return http.head(self.url).etag

    The value of remote_etag through the run flow of one batch:

        PRE_RUN_CHECKABILITY   # cache reset; first access computes "abc"
        PRE_RUN_CHECK          # reads the cached "abc"
        RUNNABILITY, RUN       # same pass, still "abc"; run() uploads
        POST_RUN_CHECKABILITY  # cache reset; a new access computes "xyz"
        POST_RUN_CHECK         # reads "xyz", the state after the run

    The pre-run pass shares one value, so the check and the run cannot
    disagree. The post-run pass starts empty, because the completion is
    verified against the real state after the run, never against a value
    that was cached before run().
    """

    def __init__(self, fget):
        self.fget = fget

    def __set_name__(self, owner, name):
        self._attr_name = name

    def _phase_cache(self, obj) -> dict:
        from winslow.task.context import (
            get_execution_context,
        )  # a lazy import, to prevent a circular import

        ctx = get_execution_context()
        if ctx is None or ctx.batch_uuid is None:
            raise RuntimeError(
                f"'{self._attr_name}' is a transient_property and can only be accessed within a batch execution context"
            )
        return phase_cache(obj, ctx.batch_uuid)

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        cache = self._phase_cache(obj)
        if self._attr_name not in cache:
            cache[self._attr_name] = self.fget(obj)
        return cache[self._attr_name]


transient_property = TransientProperty


def declared_transient_properties(klass):
    """The names of each transient_property in the MRO of a class.

    Two rules apply, the same member rules that the Attributes view
    applies to its other descriptors:

    - The most derived definition of a name decides. The name counts only
      if that definition is a transient_property.
    - A name with an underscore prefix is skipped.

    Example:

        class Base(Task):
            @transient_property
            def remote_etag(self): ...

            @transient_property
            def _headers(self): ...

        class Sub(Base):
            @transient_property
            def payload_size(self): ...

            remote_etag = "pinned"

        declared_transient_properties(Sub)
        # ["payload_size"]
        # _headers: skipped, underscore prefix
        # remote_etag: dropped, Sub overrides it with a plain attribute
    """
    names, seen = [], set()
    for kls in klass.__mro__:
        for name, val in vars(kls).items():
            if name.startswith("_") or name in seen:
                continue
            seen.add(name)
            if isinstance(val, TransientProperty):
                names.append(name)
    return names


NOT_MATERIALIZED = "Not materialized"


def snapshot_transients(task, materialized):
    """A snapshot of the transient properties of a task for one execution
    phase, as safe strings. A property that is in the phase cache maps to
    its trimmed value. Every other declared property maps to the
    NOT_MATERIALIZED sentinel.

    Example: the task declares remote_etag and payload_size, and the phase
    accessed only remote_etag:

        snapshot_transients(task, materialized={"remote_etag": "abc"})
        # {
        #     "remote_etag": "'abc'",
        #     "payload_size": "Not materialized",
        # }
    """
    return {
        name: safe_repr(materialized[name])
        if name in materialized
        else NOT_MATERIALIZED
        for name in declared_transient_properties(type(task))
    }
