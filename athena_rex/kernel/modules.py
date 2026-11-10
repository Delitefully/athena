"""Optional modules: features that are off unless config lists them, public ones in the repo, private ones in a folder of the
human's own, outside it.

A module is a folder `<name>/` with `manifest.json` (`{"name", "title"}`) and `__init__.py`. Public modules live in
athena_rex/modules/, private ones in `rex.modules_dir` (default ~/.config/athena-rex/modules/, read only: nothing here
creates it). Config's `rex.modules` lists which load, default none, so with an empty list no module's code is imported.

A loaded module is a capability like any in athena_rex/caps/ (commands, checks, tick, on_event, services), reached the same
way through kernel/caps.py, plus the optional start, stop, status, actions and action that `athena-rex modules` and the
menu bar use (docs/modules.md). A module turned off from the menu stays in the list; that on/off is the state dir's
modules.json, so a restart keeps it. A module that won't import is kept as an error, never raised into the core.
"""
import json
import os
import re
import sys
import types
from pathlib import Path

from athena_rex.kernel import config, store

PUBLIC = Path(__file__).resolve().parent.parent / "modules"
PACKAGE = "athena_rex_modules"
STATE = "modules.json"
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_loaded = {}  # name -> (folder, module or None, error): one import per process


def listed() -> list:
    """The config list, read again whenever config.json changed, so a long-running watch sees `modules load` by another
    process. Anything that isn't a name is skipped."""
    config.refresh()
    names = config.rex("modules")
    out = []
    for name in names if isinstance(names, list) else []:
        if isinstance(name, str) and name not in out:
            out.append(name)
    return out


def private_dir() -> Path:
    return Path(os.path.expanduser(str(config.rex("modules_dir"))))


def _complete(folder: Path) -> bool:
    return (folder / "manifest.json").is_file() and (folder / "__init__.py").is_file()


def locate(name: str) -> tuple:
    """(folder, public, error): where a module named this lives. A name in both places is an error, not a quiet pick."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return None, False, f"{name!r} is not a module name: lowercase letters, digits, - and _"
    public, private = PUBLIC / name, private_dir() / name
    found = [(folder, folder == public) for folder in (public, private) if _complete(folder)]
    if len(found) > 1:
        return None, False, f"named twice: {public} and {private}"
    if not found:
        return None, False, f"no module {name} in {PUBLIC} or {private_dir()}"
    return found[0][0], found[0][1], None


def manifest(folder) -> dict:
    data = store.read_json(Path(folder) / "manifest.json", None) if folder else None
    return data if isinstance(data, dict) else {}


def load(name: str) -> tuple:
    """(module or None, error). Imported once per process, from its own folder, as package athena_rex_modules.<name>, so its
    files can import each other relatively."""
    folder, public, error = locate(name)
    cached = _loaded.get(name)
    if cached and cached[0] == folder:
        return cached[1], cached[2]
    if error:
        return None, error
    if manifest(folder).get("name") != name:
        return None, f"{folder / 'manifest.json'} does not name it {name}"
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = []
        sys.modules[PACKAGE] = package
    full = f"{PACKAGE}.{name.replace('-', '_')}"
    import importlib.util  # here, not at the top: it brings typing, and a hot command loads no module
    for key in [k for k in sys.modules if k == full or k.startswith(full + ".")]:
        del sys.modules[key]  # a module found in a new folder brings its own files, not the old folder's
    try:
        spec = importlib.util.spec_from_file_location(full, folder / "__init__.py", submodule_search_locations=[str(folder)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken module is its own problem: the core runs on, and doctor and the menu say so
        for key in [k for k in sys.modules if k == full or k.startswith(full + ".")]:
            del sys.modules[key]
        module, error = None, f"won't import: {type(exc).__name__}: {exc}"
    else:
        module.MODULE = name
        module.PUBLIC = public
        module.FOLDER = folder
        if not hasattr(module, "SUMMARY"):
            module.SUMMARY = manifest(folder).get("title") or name
    _loaded[name] = (folder, module, error)
    return module, error


def _off() -> list:
    off = store.read_dict(store.path(STATE)).get("off")
    return [n for n in off if isinstance(n, str)] if isinstance(off, list) else []


def is_on(name: str) -> bool:
    return name not in _off()


def set_on(name: str, on: bool):
    with store.locked("locks/modules.lock"):
        off = [n for n in _off() if n != name]
        if not on:
            off.append(name)
        store.write_json(store.path(STATE), {"off": sorted(off)})


def active() -> list:
    """[(name, module)] listed, imported and on: what doctor, the watch and the CLI reach."""
    off = _off()
    out = []
    for name in listed():
        if name in off:
            continue
        module, _ = load(name)
        if module is not None:
            out.append((name, module))
    return out


def get(name: str):
    if name not in listed() or not is_on(name):
        return None
    return load(name)[0]


def write_list(names: list):
    """`rex.modules` in config.json, the rest of the file as it was. A config that doesn't parse is never written over."""
    with store.locked("locks/config-write.lock"):
        config.reset()
        config.load()
        if config.damaged:
            raise ValueError(f"{config.config_file()} does not parse: fix it before changing its module list")
        target = config.config_file()
        data = store.read_json(target, None)
        data = data if isinstance(data, dict) else {}
        rex = data.get("rex") if isinstance(data.get("rex"), dict) else {}
        data["rex"] = {**rex, "modules": list(names)}
        store.write_json(target, data, indent=2)
        config.reset()


def public_names() -> list:
    try:
        return sorted(e.name for e in PUBLIC.iterdir() if _complete(e))
    except OSError:
        return []


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=False)
