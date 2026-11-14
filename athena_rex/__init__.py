"""athena's old name. A private module written while athena was called athena-rex imports `athena_rex.kernel` and the like: each
such name is the very module object of its `athena` name, so there is one copy of every module and of its state."""
import importlib
import importlib.abc
import importlib.util
import sys

OLD, NEW = __name__, "athena"


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self):
        self._specs = {}

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(OLD + "."):
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        module = importlib.import_module(NEW + spec.name[len(OLD):])
        self._specs[spec.name] = module.__spec__  # the import system sets the alias's spec on it next; exec_module puts this back
        return module

    def exec_module(self, module):
        module.__spec__ = self._specs.pop(module.__spec__.name)


if not any(isinstance(finder, _Alias) for finder in sys.meta_path):
    sys.meta_path.insert(0, _Alias())
