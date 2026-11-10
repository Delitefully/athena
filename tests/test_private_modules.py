"""A private module's own tests, run here with this harness. scripts/test.sh names the human's private folder in
ATHENA_TEST_MODULES_DIR (ATHENA_REX_MODULES_DIR, else ~/.config/athena-rex/modules if it exists, read only); each
`<module>/tests/test_*.py` in it is loaded. With none named there is nothing to run, and that is said once."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

DIR = os.environ.get("ATHENA_TEST_MODULES_DIR") or ""


def found() -> list:
    return sorted(Path(DIR).glob("*/tests/test_*.py")) if DIR and os.path.isdir(DIR) else []


def load_tests(loader, standard, pattern):
    suite = unittest.TestSuite()
    paths = found()
    if not paths:
        print("private modules: none named, so none of their tests ran", file=sys.stderr)
    for path in paths:
        name = f"private_{path.parent.parent.name}_{path.stem}".replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite
