"""The contract. A capability is a module or package in athena_rex/caps/ with:

    SUMMARY          one line
    commands()       {verb: Command}, the CLI it adds

and any of these, each found by name and each optional:

    checks(ctx)      [Check]: what doctor looks at and may repair
    services(ctx)    [Service]: what runs in the background, for `services` to install
    tick(ctx, now)   called on every watch loop; returns watch Lines to tell
    on_event(ctx, e) called by the watch loop for each new bus event
    on_quit(ctx)     called by `services stop` (Quit athena) once the watch is down; returns a line to say, or None

Nothing in the kernel names a capability. Which ones load comes from config (`rex.caps`, default all found), so one
can be left out of the list, or its file deleted, and nothing else changes: other capabilities reach it only through
`ctx.cap(name)`, which returns None when it is absent.

Optional modules (kernel/modules.py, `rex.modules`, default none) are capabilities too once loaded and on: they come after
the core in every list here, so a module's verb never shadows a core one.
"""
import importlib
import os
from collections import namedtuple

from athena_rex.kernel import config, modules

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "caps")


Command = namedtuple("Command", "help configure run hidden", defaults=(False,))
Check = namedtuple("Check", "name ok detail repair grace", defaults=("", None, 0.0))
Service = namedtuple("Service", "name argv every keep_alive interactive", defaults=(None, True, False))


def found() -> list:
    names = []
    for entry in sorted(os.listdir(_DIR)):
        if entry.startswith(("_", ".")):
            continue
        base = entry[:-3] if entry.endswith(".py") else entry
        if entry.endswith(".py") or os.path.isfile(os.path.join(_DIR, entry, "__init__.py")):
            names.append(base)
    return names


def enabled() -> list:
    wanted = config.rex("caps")
    names = found()
    return names if wanted is None else [n for n in names if n in wanted]


def is_enabled(name: str) -> bool:
    wanted = config.rex("caps")
    return (wanted is None or name in wanted) and (
        os.path.exists(os.path.join(_DIR, name + ".py")) or os.path.isdir(os.path.join(_DIR, name)))


def load(name: str):
    return importlib.import_module(f"athena_rex.caps.{name}")


def get(name: str):
    return load(name) if is_enabled(name) else modules.get(name)


def loaded() -> list:
    return [load(n) for n in enabled()] + [module for _, module in modules.active()]


def find_command(verb: str):
    """The module named after the verb first, so a hot command imports only itself."""
    for name in (verb, verb + "s"):
        if is_enabled(name):
            command = load(name).commands().get(verb)
            if command:
                return command
    for name in enabled():
        command = load(name).commands().get(verb)
        if command:
            return command
    for _, module in modules.active():
        command = getattr(module, "commands", dict)().get(verb)
        if command:
            return command
    return None


class Ctx:
    """What a capability is handed: the kernel, without importing the world."""

    def __init__(self):
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            from athena_rex.kernel import backend
            self._backend = backend.load()
        return self._backend

    def cap(self, name: str):
        return get(name)


CTX = Ctx()
