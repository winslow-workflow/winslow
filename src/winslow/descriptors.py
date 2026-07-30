from dataclasses import dataclass, field, InitVar
from typing import Any, Optional, Tuple, List, Callable, Union, Type

from winslow.constants import ParameterStyle

from winslow.exceptions import ParameterizationError
from winslow.util import to_tuple


@dataclass
class Parameter:
    """
    Declare a parameter on a Task class. An instance is a Python descriptor.
    Access on the class returns the Parameter. Access on an instance returns the
    resolved value from task._params.

    One Parameter usually binds one task attribute. `from_tuple` and `from_dict`
    declare a compound parameter, which binds more than one attribute. See those
    classmethods. `name` binds the value to an attribute with a different name
    than the declared one.
    """

    values: Union[Callable, Any, None] = None
    allowed_types: Optional[Tuple[type, ...]] = None
    param_style: ParameterStyle = ParameterStyle.SEQUENTIAL
    name: Optional[str] = None

    # An InitVar, to process the raw initialization value.
    _raw_values: InitVar[Union[Callable, Any, None]] = None

    # Markers for a compound parameter (from_tuple or from_dict). These are plain
    # class attributes and not fields.
    # _compound: this Parameter expands into more than one attribute.
    # _compound_kind: "tuple" or "dict".
    # _compound_names: the explicit attribute names from names= in from_tuple, or
    # None to derive them from the declared name.
    # _compound_name_map: the {dict-key: attribute-name} translation from
    # from_dict, or None.
    _compound = False
    _compound_kind = None
    _compound_names = None
    _compound_name_map = None

    def __post_init__(self, _raw_values):
        if _raw_values is None:
            _raw_values = self.values

        if (
            _raw_values is not None
            and not callable(_raw_values)
            and self.allowed_types is not None
        ):
            for value in to_tuple(_raw_values):
                if not isinstance(value, self.allowed_types):
                    raise ParameterizationError(
                        f"{value} is not of type: {self.allowed_types}."
                    )

    @classmethod
    def from_tuple(cls, values, names=None):
        """Declare more than one attribute from rows of aligned values.

        `foo_bar = Parameter.from_tuple([(1, 2), (3, 4)])` gives two tasks:
        `(foo=1, bar=2)` and `(foo=3, bar=4)`. The attribute names come from the
        declared name, which is split on `_`, or from `names=("foo", "bar")`.
        `values` is a list of rows, or a callable(workflow_config) that returns
        one. The rows are aligned, so each row is one combination. They combine
        with each other parameter as an independent cartesian axis.
        """
        param = cls(values=values)
        param._compound = True
        param._compound_kind = "tuple"
        param._compound_names = tuple(names) if names is not None else None
        return param

    @classmethod
    def from_dict(cls, values, name_map=None):
        """Declare more than one attribute from rows of dicts.

        `foo_bar = Parameter.from_dict([{"foo": 1, "bar": 2}, {"foo": 3, "bar": 4}])`
        gives two tasks: `(foo=1, bar=2)` and `(foo=3, bar=4)`. The attribute
        names come from the declared name, as in `from_tuple`. Each row must have
        the same keys. If the dict keys are different from the attribute names,
        pass `name_map={dict_key: attribute_name}` to translate them. `values` is
        a list of dicts, or a callable(workflow_config) that returns one. The rows
        combine with each other parameter as an independent cartesian axis.
        """
        param = cls(values=values)
        param._compound = True
        param._compound_kind = "dict"
        param._compound_name_map = dict(name_map) if name_map is not None else None
        return param

    def __set_name__(self, owner, name):
        self._attr_name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(obj._params, self._attr_name)

    def __set__(self, obj, value):
        raise ParameterizationError(
            f"'{self._attr_name}' is a parameter and cannot be assigned."
        )


class _ParameterMember:
    """Descriptor for one attribute of a compound parameter (from_tuple).

    A compound Parameter has one declared name, for example `foo_bar`, but it
    binds more than one attribute. The metaclass replaces it with one
    _ParameterMember for each attribute, so `task.foo` and `task.bar` resolve
    from the params of the instance.
    """

    def __init__(self, name):
        self._attr_name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(obj._params, self._attr_name)

    def __set__(self, obj, value):
        raise ParameterizationError(
            f"'{self._attr_name}' is a parameter and cannot be assigned."
        )


@dataclass
class ConfigOption:
    type: Optional[Type] = None
    default: Optional[Any] = None
    # The inverse of `type`. str() cannot invert it for a value that is not a
    # scalar: a list default becomes "[1]", which does not parse again. An option
    # can thus supply its own serializer from a value to a string.
    serializer: Optional[Callable] = None
    help_text: str = ""
    required: bool = False
    choices: Optional[List[Any]] = None
    multiselect: bool = False
    action: Optional[str] = None
    const: Optional[Any] = None  # used only if action == "store_const"
    show_on_ui: bool = True
    # Part of the display label of the instance. It implies required.
    identifier: bool = False
    subcommands: Tuple[Any, ...] = field(default_factory=tuple)
    depends_on: Tuple[str, ...] = field(default_factory=tuple)
    name: Optional[str] = None

    def __post_init__(self):
        self.subcommands = to_tuple(self.subcommands) if self.subcommands else tuple()
        self.depends_on = to_tuple(self.depends_on) if self.depends_on else tuple()
        if self.identifier:
            # An identifier with no value distinguishes nothing, so the user must
            # supply it.
            self.required = True

    def format_value(self, value):
        if value is None:
            return None
        if self.serializer:
            return self.serializer(value)
        if isinstance(value, (list, tuple)):
            # A multiselect value is a list. str() of a list is noisy in a label.
            return ", ".join(str(v) for v in value)
        return str(value)

    def to_arg(self, name=None, lenient=False):
        """Make the argparse argument definition. `lenient` removes `required`, so
        a parser that is shared between workflows does not abort on a mandatory
        option of one workflow. The strict parse of a single workflow enforces the
        value."""
        name = (name or self.name).replace("_", "-")

        common = dict(
            arg_name=f"--{name}",
            help=self.help_text,
            # A default supplies the value, so the command line does not demand
            # the option again.
            required=self.required and self.default is None and not lenient,
            default=self.default,
        )

        if self.action:
            if self.action == "store_const":
                return dict(common, action=self.action, const=self.const)
            else:
                return dict(common, action=self.action)

        result = dict(
            common,
            type=self.type,
            choices=self.choices,
        )
        if self.multiselect:
            # The form takes more than one value, so the command line must
            # take them too.
            result["nargs"] = "+"
        return result
