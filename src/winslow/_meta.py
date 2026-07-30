class _DeclarationMeta(type):
    """Base metaclass that collects the class-level declarations through the MRO.

    A subclass calls _collect() to get the declarations of one type from the base
    classes. The bases come first and the subclass last, so the subclass always
    wins a name clash.
    """

    @classmethod
    def _collect(cls, bases, dct, decl_type, meta_attr):
        result = {}
        for base in reversed(bases):
            if hasattr(base, meta_attr):
                result.update(getattr(base, meta_attr))
        result.update({k: v for k, v in dct.items() if isinstance(v, decl_type)})
        return result


def _check_attribute_clashes(
    cls_name, bases, dct, attr_names, *, exempt_types, error_cls, noun
):
    """A declaration must not bind a name that has a meaning on the class.

    The collection of the Parameters and of the ConfigOptions shares this
    function. The owner of the name decides what "taken" means:

    - A framework name, which a winslow class in the MRO declares, is reserved by
      its presence and independent of its value. `check = ConfigOption(...)` on a
      workflow is thus rejected, although it would only shadow Workflow.check.
    - A user name follows a placeholder convention. A name that is present with
      the value None is a placeholder, and an override is correct. Each other
      value is a clash.

          class ShardedBase(Task):
              shard = None                      # placeholder, an override is ok

          class Sharded(ShardedBase):
              shard = Parameter(values=[1, 2])  # ok
              check = Parameter(values=[3])     # rejected: Task.check exists

    A declaration of the same kind (`exempt_types`) is exempt. It is an inherited
    member, or a new declaration of that member, and not a clash. `error_cls` and
    `noun` let each kind of declaration report the clash in its own words.
    """
    sources = [(cls_name, dct, False)]
    sources += [
        (kls.__name__, vars(kls), kls.__module__.split(".")[0] == "winslow")
        for base in bases
        for kls in base.__mro__
    ]
    for attr in attr_names:
        for owner, members, is_framework in sources:
            if attr not in members or isinstance(members[attr], exempt_types):
                continue
            if is_framework or members[attr] is not None:
                raise error_cls(
                    f"{cls_name}: {noun} '{attr}' clashes with existing "
                    f"attribute {owner}.{attr}."
                )
