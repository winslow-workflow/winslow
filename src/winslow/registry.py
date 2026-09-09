import collections

from winslow._base import _Base
from winslow.util import get_is_abstract, to_tuple, iter_dir_modules, classes_in_module
from winslow.exceptions import RegistrationError
from winslow.logger import LOGGER


class Registry(_Base):
    item_class = None
    # file_filter holds file names, dir_filter holds directory names. A module
    # is scanned when either filter matches it (see util._module_matches).
    file_filter = None
    dir_filter = None

    def __init__(self, orchestrator_config):
        super().__init__(orchestrator_config)

        self._name_registry = {}

        # More than one class can have the same name.
        self._classname_registry = collections.defaultdict(set)

    def __contains__(self, name):
        return name in self._name_registry

    def __getitem__(self, name):
        return self._name_registry[name]

    @property
    def names(self):
        return sorted(self._name_registry.keys())

    @property
    def classes(self):
        return self._name_registry.values()

    def _should_register_klass(self, klass):
        """Register only a concrete class of the matching type or of a subtype."""
        return issubclass(klass, self.item_class) and not get_is_abstract(klass)

    def get_registration_context(self):

        # target, key getter, is_strict
        return [
            (self._name_registry, lambda obj: obj.get_name(), True),
            (self._classname_registry, lambda obj: obj.__name__, False),
        ]

    def _register_strict(self, key, value, target):
        if key in target:
            if value is not target[key]:
                raise RegistrationError(
                    f"cannot register {key!r}: {target[key]} already holds that name - "
                    f"rename one of the two classes."
                )
            else:
                LOGGER.info(
                    f"{value} (id: {id(value)}) has already been collected by {self}."
                )
        else:
            target[key] = value

    def _register_non_strict(self, key, value, target):
        target[key].add(value)

    def register(self, klass):

        for target, getter, is_strict in self.get_registration_context():
            # If the key getter returns more than one value, the class is added
            # to the registry under each key.
            keys = to_tuple(getter(klass))
            setter = self._register_strict if is_strict else self._register_non_strict

            for key in keys:
                setter(key, klass, target)

    def collect_classes(self, directory):
        LOGGER.debug(f"{self} collecting {self.item_class} classes from {directory}.")
        for module in iter_dir_modules(
            directory, only=self.file_filter, under=self.dir_filter
        ):
            for kls in classes_in_module(module, self.item_class):
                self.register(kls)
        LOGGER.debug(f"Collection complete - {len(self.classes)} items collected.")
