# Parameterization

A `Parameter` declaration turns one task class into many task instances. Each instance holds its own values,
its own status, and its own place in the dependency graph. Winslow tracks and runs each instance on its own.

## Declare a parameter

```python
from winslow import Task, Parameter


class Extract(Task):
    region = Parameter(values=["eu", "us"])

    def run(self):
        fetch_rows(region=self.region)
```

Each value gives one instance: `extract ('eu')` and `extract ('us')`. The label of an instance shows its
values, in the UI and in the logs.

A task reads the value from the attribute, here `self.region`, in any method. The value is read-only, and an
assignment raises a `ParameterizationError`.

## More than one parameter

Two parameters zip into rows by default. The first values go together, and the second values go together:

```python
class Load(Task):
    src = Parameter(values=["a", "b"])
    dst = Parameter(values=["x", "y"])
```

This gives two instances: `load ('a', 'x')` and `load ('b', 'y')`. The value lists must have the same length,
and Winslow rejects a mismatch with a `ParameterizationError`.

### The product style

The product style gives every combination:

```python
from winslow.constants import ParameterStyle


class Matrix(Task):
    region = Parameter(values=["eu", "us"], param_style=ParameterStyle.PRODUCT)
    shard = Parameter(values=[1, 2], param_style=ParameterStyle.PRODUCT)
```

This gives four instances: `matrix ('eu', 1)`, `matrix ('eu', 2)`, `matrix ('us', 1)` and `matrix ('us', 2)`.

The two styles combine. The sequential parameters zip into one axis. Each product parameter is its own axis,
and the instances are the product of the axes:

```python
class Mixed(Task):
    src = Parameter(values=["a", "b"])
    dst = Parameter(values=["x", "y"])
    region = Parameter(values=["eu", "us"], param_style=ParameterStyle.PRODUCT)
```

The two rows and the two regions give four instances: `mixed ('a', 'x', 'eu')`, `mixed ('a', 'x', 'us')`,
`mixed ('b', 'y', 'eu')` and `mixed ('b', 'y', 'us')`.

## Values from the workflow config

The `values` argument also takes a function. Winslow calls it with the workflow config at startup:

```python
class FetchDay(Task):
    day = Parameter(values=lambda cfg: list(range(1, cfg.days + 1)))
```

The option `--days 5` then gives five instances. The number of instances follows the config of the run.

## Full control with get_parameters

The `get_parameters` classmethod builds the parameter sets in code. It returns a list of dicts, and each dict
is one instance:

```python
class Report(Task):
    region = Parameter()
    limit = Parameter()

    @classmethod
    def get_parameters(cls, workflow_config):
        return [
            {"region": "eu", "limit": 10},
            {"region": "us", "limit": 20},
        ]
```

Use this method when the sets do not fit a `values` declaration: a lookup in a file, a query, or a rule that
couples the values.

!!! info "get_parameters has priority"

    When a class declares `get_parameters`, Winslow ignores the `values` arguments of its parameters. A
    parameterized class must supply the values in one of the two ways, or the startup fails.

## Compound parameters

One `from_tuple` declaration binds more than one attribute from aligned rows:

```python
class Sync(Task):
    src_dst = Parameter.from_tuple([("a", "x"), ("b", "y")])

    def run(self):
        copy(self.src, self.dst)
```

Each row is one instance: `sync ('a', 'x')` and `sync ('b', 'y')`.

!!! info "The declared name sets the attribute names"

    Winslow splits the declared name `src_dst` on the underscore, and it binds the attributes `src` and
    `dst`. This split is implicit. The task reads `self.src` and `self.dst`, and the declared name itself
    disappears: `self.src_dst` does not exist. Pass `names=(...)` to set the attribute names explicitly.

The `from_dict` form takes rows of dicts:

```python
class Push(Task):
    host_port = Parameter.from_dict(
        [{"h": "alpha", "p": 80}, {"h": "beta", "p": 81}],
        name_map={"h": "host", "p": "port"},
    )
```

Each row must have the same keys. The `name_map` argument translates a dict key to an attribute name. Leave it
out when the keys already are the attribute names.

Both forms also take a function for the rows, with the workflow config as the argument. A compound parameter
is one axis, like the zipped sequential parameters, and it crosses with the other axes as a product:

```python
class Copy(Task):
    region = Parameter(values=["eu", "us"], param_style=ParameterStyle.PRODUCT)
    src_dst = Parameter.from_tuple(lambda cfg: [("a", "x"), ("b", "y")])
```

The two rows and the two regions give four instances: `copy ('eu', 'a', 'x')`, `copy ('eu', 'b', 'y')`,
`copy ('us', 'a', 'x')` and `copy ('us', 'b', 'y')`.

## The rules for the values

Winslow validates the parameter sets at startup, and a violation raises a `ParameterizationError`:

- Each set must be unique. Two instances of one class cannot hold the same values.
- Each value must be hashable, because the identity of an instance uses it.
- The `allowed_types` argument restricts the type of a value, for example `allowed_types=(int,)`.

## Drop an instance before creation

The `should_be_initialized` classmethod receives each parameter set before Winslow creates the instance.
Return false to drop the set:

```python
class Audit(Task):
    region = Parameter(values=["eu", "us", "legacy"])

    @classmethod
    def should_be_initialized(cls, workflow_config, parameters=None):
        return parameters.region != "legacy"
```

This is stronger than `is_eligible`, which marks a created instance as skipped. Use it when most of the sets
are not relevant for the run.

!!! warning "A dropped instance is invisible"

    An instance that is not created does not show on the UI, with no status and no reason. Prefer
    `is_eligible` when the user must see the decision.

## Order the instances

A dependency between two parameterized classes reaches every instance pair. The `depends_on` method filters
the pairs. Pair the instances on a shared parameter:

```python
class Publish(Task):
    region = Parameter(values=["eu", "us"])
    dependencies = RegionReport

    def depends_on(self, task):
        return task.region == self.region
```

`publish ('eu')` then depends on `region-report ('eu')` only.

The `"self"` dependency points a class at its own instances. With a `depends_on` filter it chains them:

```python
class FetchDay(Task):
    day = Parameter(values=[1, 2, 3])
    dependencies = "self"

    def depends_on(self, task):
        return task.day == self.day - 1
```

Day 2 depends on day 1, and day 3 depends on day 2. The days thus run in order. See
[Dependencies](dependencies.md) for the dependency forms.

## A complete example

The `metrics` workflow puts the pieces together. The config options set the days and the regions, so the
instances follow the command line. Each region builds one report from every fetched day, and each publish
pairs with the report of its own region.

```python title="examples/metrics/workflow.py"
--8<-- "examples/metrics/workflow.py"
```

[Download this example](https://github.com/winslow-workflow/winslow/blob/main/examples/metrics/workflow.py)

The declarations resolve to these instances and dependencies:

| The instance | It depends on |
| --- | --- |
| `fetch-day (1)` | Nothing. |
| `fetch-day (2)` | `fetch-day (1)` |
| `fetch-day (3)` | `fetch-day (2)` |
| `region-report ('eu')` | Every fetch-day instance. |
| `region-report ('us')` | Every fetch-day instance. |
| `publish ('eu')` | `region-report ('eu')` |
| `publish ('us')` | `region-report ('us')` |

Run it headless with `winslow run --mode headless --workflow metrics`. Pass `--days 5` and the fetch chain
grows to five instances. Pass `--regions eu us apac` and a third report and publish pair appears. The regions
option is an identifier, so the label of the workflow instance shows the selection, for example
`regions=eu us` (see [the identifier options](workflows.md#the-identifier-options)). Each task writes a
marker into the `state` directory. Delete the directory to run the flow again.
