import networkx as nx

from winslow._base import _Base
from winslow._meta import _DeclarationMeta, _check_attribute_clashes
from winslow.descriptors import ConfigOption
from winslow.exceptions import MisconfigurationError


class _ConfigMeta(_DeclarationMeta):
    def __new__(cls, name, bases, dct):
        config_options = cls._collect(bases, dct, ConfigOption, "config_meta")

        _check_attribute_clashes(
            name,
            bases,
            dct,
            tuple(config_options.keys()),
            exempt_types=(ConfigOption,),
            error_cls=MisconfigurationError,
            noun="config option",
        )

        for opt_name, option in config_options.items():
            for dep in option.depends_on:
                if dep not in config_options:
                    raise MisconfigurationError(
                        f"{name}.{opt_name} declares depends_on={dep!r}, which is not a config option."
                    )
                if option.default and not config_options[dep].default:
                    raise MisconfigurationError(
                        f"{name}.{opt_name} defaults to set but depends on {dep!r}, which defaults "
                        f"to unset - the declaration's own defaults violate the dependency."
                    )

        cls._check_cyclical_option_dependencies(name, config_options)

        result = super().__new__(cls, name, bases, dct)

        result.config_meta = config_options
        result.config_option_names = tuple(config_options.keys())

        return result

    @classmethod
    def _check_cyclical_option_dependencies(cls, name, config_options):
        """Reject a depends_on chain that is cyclic, and also a self-dependency.
        This is the config-option equivalent of
        Graph._check_cyclical_dependencies."""
        nx_graph = nx.DiGraph()
        for opt_name, option in config_options.items():
            for dep in option.depends_on:
                nx_graph.add_edge(opt_name, dep)

        for cycle in nx.simple_cycles(nx_graph):
            chain = " -> ".join(cycle + [cycle[0]])
            raise MisconfigurationError(
                f"{name}: cyclical config option dependency: {chain}."
            )


class _ConfigBase(_Base, metaclass=_ConfigMeta):
    @classmethod
    def validate_option_dependencies(cls, config, subcommand=None):
        """Enforce ConfigOption(depends_on=...). An option that is set demands
        that its dependencies are set.

        "Set" means truthy, so a default counts as a value. _ConfigMeta rejects
        a declaration whose own defaults break a dependency.

        Example: filter declares depends_on="initialize", and initialize
        applies only to the show subcommand. The check walks these steps:

        1. Skip filter when the config holds no value for it.
        2. Look up each dependency of filter, here initialize.
        3. Skip a dependency outside its subcommands. Under run, initialize
           does not apply, so the check accepts the config.
        4. Under show, raise MisconfigurationError when initialize is unset.

        Step 3 must test dep.subcommands, not the value. Trace the valid
        command "winslow run --filter foo":

        - The parser writes the defaults of every subcommand onto the
          namespace, so config.initialize is False under run as well.
        - A test on the value alone reads that False and rejects the command.
        - dep.subcommands shows that initialize belongs only to show, so
          step 3 skips the dependency and the command passes."""
        for opt_name, option in cls.config_meta.items():
            if not option.depends_on or not getattr(config, opt_name):
                continue
            for dep_name in option.depends_on:
                dep = cls.config_meta[dep_name]
                if dep.subcommands and subcommand not in dep.subcommands:
                    continue
                if not getattr(config, dep_name):
                    raise MisconfigurationError(
                        f"--{opt_name.replace('_', '-')} can only be used with "
                        f"--{dep_name.replace('_', '-')}."
                    )

    @classmethod
    def get_argparse_context(cls, subcommand=None, lenient=False):
        """
        Return a list of kwargs for the argparse 'add_argument' method, built
        from the ConfigOption declarations.
        """

        return [
            conf.to_arg(conf_name, lenient=lenient)
            for conf_name, conf in cls.config_meta.items()
            if (subcommand and subcommand in conf.subcommands)
            or (subcommand is None and not conf.subcommands)
        ]
