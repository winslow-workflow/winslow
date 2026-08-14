class CacheContainer:
    """A read-only namespace of cache instances, by cache name. It holds no
    public members of its own, so a cache name cannot clash with it."""

    def __init__(self, instances):
        object.__setattr__(self, "_instances", dict(instances))

    def __getattr__(self, name):
        try:
            return self._instances[name]
        except KeyError:
            raise AttributeError(
                f"Unknown cache '{name}' - known caches: {sorted(self._instances)}"
            ) from None

    def __setattr__(self, name, value):
        raise AttributeError(
            f"The cache container is read-only - cannot assign '{name}'."
        )

    def __repr__(self):
        return f"<CacheContainer {sorted(self._instances)}>"


class CacheContainerRef:
    """The cache container of one scope, on Task and Graph. A descriptor, not a
    property, so the attributes view drops it (compare _ExecutionFlag)."""

    def __init__(self, stamp_attr, fallback):
        self._stamp_attr = stamp_attr
        self._fallback = fallback

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        stamped = getattr(obj, self._stamp_attr)
        if stamped is not None:
            return stamped
        return self._fallback()

    def __set__(self, obj, value):
        raise AttributeError(f"'{self._name}' is read-only.")
