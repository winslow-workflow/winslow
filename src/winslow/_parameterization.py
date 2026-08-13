"""The parameterization logic. It is in this module to keep the graph clear."""

import collections
import uuid as _uuid
from functools import cached_property
from itertools import product
from types import SimpleNamespace

from winslow._base import _Base
from winslow._meta import _DeclarationMeta, _check_attribute_clashes
from winslow.constants import ParameterStyle
from winslow.exceptions import (
    ParameterizationError,
)

from winslow.descriptors import Parameter, _ParameterMember
from winslow.util import (
    is_tuple_like,
    to_tuple,
    safe_repr,
)


class _GetParametersNotImplemented(NotImplementedError):
    # Task.get_parameters raises this sentinel if a subclass does not override
    # it. The code catches this class and not NotImplementedError. A real
    # NotImplementedError from an overridden get_parameters thus propagates.
    pass


def _resolve_singular_name(cls_name, declared_name, param):
    if param.name is None:
        return declared_name
    if not param.name.isidentifier() or param.name.startswith("_"):
        raise ParameterizationError(
            f"{cls_name}: parameter '{declared_name}' has an invalid name override ({param.name!r})."
        )
    return param.name


def _resolve_compound_names(cls_name, declared_name, param):
    """The attribute names that a compound parameter binds. These are the
    explicit names from `names=` in from_tuple. If there are none, the
    declared name is split on '_'.

    Names derived from the declared name:

        host_port = Parameter.from_tuple(rows)
        # binds ("host", "port")

    Explicit names:

        location = Parameter.from_tuple(rows, names=("region", "zone"))
        # binds ("region", "zone"); the declared name binds nothing
    """
    names = param._compound_names or tuple(declared_name.split("_"))
    if len(names) < 2:
        raise ParameterizationError(
            f"{cls_name}: compound parameter '{declared_name}' needs at least two "
            f"attribute names; splitting on '_' gave {names}. Pass names=(...) explicitly."
        )
    invalid = [n for n in names if not n.isidentifier() or n.startswith("_")]
    if invalid:
        raise ParameterizationError(
            f"{cls_name}: compound parameter '{declared_name}' has invalid attribute name(s) {invalid}."
        )
    return tuple(names)


class _ParameterizationMeta(_DeclarationMeta):
    def __new__(cls, name, bases, dct):
        parameters = cls._collect(bases, dct, Parameter, "_parameterization_meta")

        underscore_names = [n for n in parameters.keys() if n.startswith("_")]
        if underscore_names:
            raise ParameterizationError(
                f"{name}: Parameter name(s) {sorted(underscore_names)} must not start with an underscore."
            )

        # Flatten to the effective attribute names. A compound parameter has one
        # declared name, but it binds more than one attribute.
        attr_names = []
        for declared_name, param in parameters.items():
            if getattr(param, "_compound", False):
                param._resolved_names = _resolve_compound_names(
                    name, declared_name, param
                )
                attr_names.extend(param._resolved_names)
            else:
                attr_names.append(_resolve_singular_name(name, declared_name, param))

        duplicates = sorted({n for n in attr_names if attr_names.count(n) > 1})
        if duplicates:
            raise ParameterizationError(
                f"{name}: duplicate parameter attribute name(s) {duplicates} - compound "
                f"parameter names must not collide with other parameters."
            )

        _check_attribute_clashes(
            name,
            bases,
            dct,
            attr_names,
            exempt_types=(Parameter, _ParameterMember),
            error_cls=ParameterizationError,
            noun="parameter attribute",
        )

        for declared_name, param in parameters.items():
            if declared_name not in dct:
                continue  # inherited and already expanded on the base
            if getattr(param, "_compound", False):
                del dct[declared_name]
                for member_name in param._resolved_names:
                    dct[member_name] = _ParameterMember(member_name)
            elif param.name and param.name != declared_name:
                del dct[declared_name]
                dct[param.name] = param  # __set_name__ binds _attr_name again

        dct["_is_parameterized"] = bool(parameters)
        dct["_parameterization_meta"] = parameters
        dct["_parameter_names"] = tuple(attr_names)

        return super().__new__(cls, name, bases, dct)


class _ParameterizationBase(_Base, metaclass=_ParameterizationMeta):
    def __init__(self, parameters: SimpleNamespace = None):
        self._params = parameters
        # The opaque transport identity: history holds this string, never the
        # task (see TaskInfo). A copy would duplicate it, so never copy a task.
        self.uuid = str(_uuid.uuid4())

    @property
    def _parameters_dict(self):
        return self._params.__dict__ if self._params else {}

    @property
    def _parameters_dict_safe(self):
        return {k: safe_repr(v) for k, v in self._parameters_dict.items()}

    @cached_property
    def _identity(self):
        return (self.instance_name, tuple(sorted(self._parameters_dict.items())))

    def __hash__(self):
        return hash(self._identity)

    def __repr__(self):
        if not self._is_parameterized:
            return f"< {self.__class__.__name__} >"
        params_str = " ".join(
            f"{name}={value}" for name, value in self._parameters_dict_safe.items()
        )
        return f"< {self.__class__.__name__} {params_str} >"

    def __eq__(self, other):
        if not isinstance(other, _ParameterizationBase):
            return NotImplemented
        return self._identity == other._identity


def _check_allowed_types(task_kls, field_name, field, value):
    allowed_types = field.allowed_types

    if allowed_types:
        if not isinstance(value, allowed_types):
            raise ParameterizationError(
                (
                    f"Value ({safe_repr(value)}) for parameter ({task_kls.__name__}.{field_name})"
                    f" is not one of the allowed types {allowed_types} - it is of type {type(value)}"
                ),
                task_kls=task_kls,
            )


def _check_hashability(task_kls, field_name, value):
    try:
        hash(value)
    except TypeError:
        raise ParameterizationError(
            f"Unhashable value generated for parameter: {field_name}. ({value})",
            task_kls=task_kls,
        )


def _fields_by_attr(task_kls):
    """Map each effective attribute name to its Parameter.

    Example declaration:

        class Deploy(Task):
            count = Parameter(values=(1, 2))
            host_port = Parameter.from_tuple(rows)

    The result:

        {
            "count": count,
            "host": host_port,
            "port": host_port,
        }

    The compound Parameter appears once for each attribute name that it
    binds."""
    mapping = {}
    for declared_name, field in task_kls._parameterization_meta.items():
        if getattr(field, "_compound", False):
            for attr in field._resolved_names:
                mapping[attr] = field
        else:
            mapping[field.name or declared_name] = field
    return mapping


def _validate_context(task_kls, context):
    """Validate the parameterization context, from get_parameters or from
    the values path. Each entry becomes one task instance.

    A valid context:

        [
            {"host": "a", "port": 80},
            {"host": "b", "port": 80},
        ]

    The checks, in order:

    1. Each entry must be a dict. An entry ("a", 80) raises.
    2. Each value must be hashable, because the task identity hashes it.
       A list value, as in {"host": ["a"]}, raises.
    3. Each value must satisfy the allowed_types of its parameter, if the
       parameter declares them.
    4. Each entry must be unique. A second {"host": "a", "port": 80}
       raises, because two tasks would share one identity.
    """
    fields = _fields_by_attr(task_kls)
    seen = set()
    for params in context:
        if not isinstance(params, dict):
            raise ParameterizationError(
                f"{task_kls.__name__}: parameterization context entries must be dicts, "
                f"got {safe_repr(params)} ({type(params).__name__}).",
                task_kls=task_kls,
            )
        for attr, value in params.items():
            _check_hashability(task_kls, attr, value)
            field = fields.get(attr)
            if field is not None and field.allowed_types is not None:
                _check_allowed_types(task_kls, attr, field, value)
        row = tuple(sorted(params.items()))
        if row in seen:
            raise ParameterizationError(
                f"duplicate entry {safe_repr(params)} - every parameterized task "
                "must have a unique and hashable parameter set.",
                task_kls=task_kls,
            )
        seen.add(row)


def _get_parameterization_context(task_kls, workflow_config):
    all_values_defined = all(
        field.values is not None for field in task_kls._parameterization_meta.values()
    )

    try:
        # An explicit function declaration has priority.
        context = task_kls.get_parameters(workflow_config)
    except _GetParametersNotImplemented:
        if not all_values_defined:
            raise
        context = _generate_context_from_params(task_kls, workflow_config)

    _validate_context(task_kls, context)
    return context


def _validate_sequential_value_lengths(task_kls, sequential_ctx):
    """The sequential parameter values must all have the same length."""

    # key: the length, value: the list of fields
    seq_param_length_map = collections.defaultdict(list)

    for field_name, values in sequential_ctx.items():
        seq_param_length_map[len(values)].append(field_name)

    if len(seq_param_length_map) > 1:
        ctx_readable = [
            f"'{field_name}': ({num_items} items)"
            for num_items, field_names in seq_param_length_map.items()
            for field_name in field_names
        ]

        raise ParameterizationError(
            f"Sequential parametrization values with different lengths were generated {', '.join(ctx_readable)}",
            task_kls=task_kls,
        )


def _validate_parameter_style_declaration(task_kls, field_name, field):
    p_style = field.param_style
    if p_style not in list(ParameterStyle):
        raise ParameterizationError(
            (
                f"Invalid parameterization type {p_style} for {field_name}. "
                f"Allowed: {list(ParameterStyle)}"
            ),
            task_kls=task_kls,
        )


def _eval_parameters(field, workflow_config):
    values = field.values

    if callable(values):
        values = values(workflow_config)

    return to_tuple(values)


def _generate_sequential_context(seq_param):
    keys = list(seq_param.keys())
    values = list(seq_param.values())
    return [dict(zip(keys, items)) for items in zip(*values)]


def _generate_product_context(pro_param):
    keys = list(pro_param.keys())
    values = list(pro_param.values())

    return [dict(zip(keys, items)) for items in product(*values)]


def _eval_rows(task_kls, field_name, field, workflow_config):
    """Materialize the rows of a compound parameter. The rows are the
    literal `values`, or the result of a call to `values` with
    workflow_config.

    Example:

        Parameter.from_tuple(lambda cfg: [(1, 2), (3, 4)])
        # rows: [(1, 2), (3, 4)]

    The result must be a sequence of rows. A scalar or a bare string
    raises."""
    rows = field.values
    if callable(rows):
        rows = rows(workflow_config)
    if not is_tuple_like(rows):
        raise ParameterizationError(
            f"{task_kls.__name__}.{field_name}: expected a sequence of rows, "
            f"got {safe_repr(rows)} ({type(rows).__name__}).",
            task_kls=task_kls,
        )
    return list(rows)


def _validate_compound_tuple(task_kls, field_name, names, rows):
    """The rows of from_tuple. Each row must be a tuple or a list, and its
    length must be equal to the number of declared attribute names.

    With names ("host", "port"):

        [("a", 80), ("b", 443)]      # passes
        [("a", 80), ("b",)]          # raises: 1 value for 2 names
        [{"host": "a", "port": 80}]  # raises: a dict row is for from_dict

    The hashability of a value is tested later, on the merged context."""
    for row in rows:
        if not isinstance(row, (tuple, list)):
            raise ParameterizationError(
                f"{task_kls.__name__}.{field_name}: from_tuple rows must be tuples, "
                f"got {safe_repr(row)} ({type(row).__name__}).",
                task_kls=task_kls,
            )
        if len(row) != len(names):
            raise ParameterizationError(
                f"{task_kls.__name__}.{field_name}: row {safe_repr(row)} has {len(row)} "
                f"value(s) but {len(names)} attribute name(s) {names} were declared.",
                task_kls=task_kls,
            )


def _resolve_dict_mapping(task_kls, field_name, names, name_map, rows):
    """Resolve the {dict_key: attribute_name} mapping for the rows of
    from_dict. Each row must be a dict, and all rows must have the same
    keys.

    Example: `host_port = Parameter.from_dict(rows)` declares the names
    ("host", "port").

    Keys equal to the names use the identity mapping:

        rows = [{"host": "a", "port": 80}]
        # mapping: {"host": "host", "port": "port"}

    Different keys need a name_map:

        rows = [{"h": "a", "p": 80}]
        # without name_map: raises
        # with name_map={"h": "host", "p": "port"}: that map applies

    A name_map must cover the dict keys exactly, and its values must map
    onto the attribute names exactly."""
    for row in rows:
        if not isinstance(row, dict):
            raise ParameterizationError(
                f"{task_kls.__name__}.{field_name}: from_dict rows must be dicts, "
                f"got {safe_repr(row)} ({type(row).__name__}).",
                task_kls=task_kls,
            )

    key_sets = {frozenset(row) for row in rows}
    if len(key_sets) > 1:
        raise ParameterizationError(
            f"{task_kls.__name__}.{field_name}: all from_dict rows must have the same keys; "
            f"found {sorted(sorted(keys) for keys in key_sets)}.",
            task_kls=task_kls,
        )

    dict_keys = set(next(iter(key_sets))) if key_sets else set()
    names_set = set(names)

    if name_map is None:
        if dict_keys != names_set:
            raise ParameterizationError(
                f"{task_kls.__name__}.{field_name}: dict keys {sorted(dict_keys)} don't match "
                f"attribute names {sorted(names_set)} - pass name_map={{dict_key: attribute}}.",
                task_kls=task_kls,
            )
        return {key: key for key in dict_keys}

    if set(name_map) != dict_keys:
        raise ParameterizationError(
            f"{task_kls.__name__}.{field_name}: name_map keys {sorted(name_map)} must match "
            f"the dict keys {sorted(dict_keys)}.",
            task_kls=task_kls,
        )
    if set(name_map.values()) != names_set:
        raise ParameterizationError(
            f"{task_kls.__name__}.{field_name}: name_map values {sorted(name_map.values())} must "
            f"map onto the attribute names {sorted(names_set)}.",
            task_kls=task_kls,
        )
    return name_map


def _compound_axis(task_kls, field_name, field, workflow_config):
    """Build one cartesian axis for a compound parameter. The axis is a list of
    partial {attr: value} dicts. The kind of the parameter, from_tuple or
    from_dict, selects the method."""
    names = field._resolved_names
    rows = _eval_rows(task_kls, field_name, field, workflow_config)

    if field._compound_kind == "dict":
        mapping = _resolve_dict_mapping(
            task_kls, field_name, names, field._compound_name_map, rows
        )
        return [{mapping[key]: value for key, value in row.items()} for row in rows]

    _validate_compound_tuple(task_kls, field_name, names, rows)
    return [dict(zip(names, row)) for row in rows]


def _generate_context_from_params(task_kls, workflow_config):
    """Build the parameterization context if each Parameter attribute has a
    'values' argument.

    The sequential parameters are zipped and must have the same length. Each
    product parameter and each compound parameter is an independent cartesian
    axis. The final context is the cartesian product of all the axes.

    Example declaration:

        class Deploy(Task):
            env = Parameter(values=("dev", "prod"))     # sequential (default)
            region = Parameter(values=("eu", "us"))     # sequential
            debug = Parameter(
                values=(True, False),
                param_style=ParameterStyle.PRODUCT,
            )

    The axes:

        # sequential, zipped into one axis
        [{"env": "dev", "region": "eu"}, {"env": "prod", "region": "us"}]
        # product
        [{"debug": True}, {"debug": False}]

    The final context, the cartesian product of the axes, merged:

        [
            {"env": "dev", "region": "eu", "debug": True},
            {"env": "dev", "region": "eu", "debug": False},
            {"env": "prod", "region": "us", "debug": True},
            {"env": "prod", "region": "us", "debug": False},
        ]
    """

    meta = task_kls._parameterization_meta

    # Separate the sequential singles from the product singles. Each compound
    # parameter is its own axis.
    seq_param, pro_param = {}, {}
    compound_axes = []

    for field_name, field in meta.items():
        if getattr(field, "_compound", False):
            compound_axes.append(
                _compound_axis(task_kls, field_name, field, workflow_config)
            )
            continue

        _validate_parameter_style_declaration(task_kls, field_name, field)

        target = (
            seq_param if field.param_style is ParameterStyle.SEQUENTIAL else pro_param
        )
        target[field.name or field_name] = _eval_parameters(field, workflow_config)

    _validate_sequential_value_lengths(task_kls, seq_param)

    # Each axis is a list of partial {name: value} dicts. The context is their
    # cartesian product, merged. With one axis the result is that axis.
    axes = []
    if seq_param:
        axes.append(_generate_sequential_context(seq_param))
    if pro_param:
        axes.append(_generate_product_context(pro_param))
    axes.extend(compound_axes)

    if not axes:
        raise ParameterizationError(
            (
                "Empty parameterization context was generated."
                " - please check value generation functions if you are using any."
            ),
            task_kls=task_kls,
        )

    return [
        {key: value for partial in combo for key, value in partial.items()}
        for combo in product(*axes)
    ]
