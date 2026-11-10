"""`athena-rex modules`: the optional modules in config's list, as the menu bar shows them, and loading, unloading and
running their actions. One JSON object per listed module, loaded or not:

    {"name", "title", "public", "loaded", "state": "on|off|error", "detail": "<one short line>", "actions": [{"id", "label"}]}

Every loaded module has the `toggle` action, which turns it off or on and keeps it in the list; a module may add its own
(docs/modules.md).
"""
import json

from athena_rex.cli import UserError
from athena_rex.kernel import caps
from athena_rex.kernel import modules as loader

SUMMARY = "the optional modules: list them, load, unload, and run their menu actions"
DETAIL_MAX = 120


def _line(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX - 1] + "…"


def _status(ctx, module) -> tuple:
    finder = getattr(module, "status", None)
    if finder is None:
        return True, "running"
    try:
        ok, detail = finder(ctx)
    except Exception as exc:  # a module that can't say how it is reads as broken, not as fine
        return False, f"status failed: {type(exc).__name__}: {exc}"
    return bool(ok), detail


def _own_actions(ctx, module) -> list:
    finder = getattr(module, "actions", None)
    try:
        found = finder(ctx) if finder else []
    except Exception:
        return []
    return [{"id": str(a["id"]), "label": _line(a.get("label") or a["id"])} for a in found or []
            if isinstance(a, dict) and a.get("id") and a["id"] != "toggle"]


def describe(ctx, name: str) -> dict:
    folder, public, error = loader.locate(name)
    title = loader.manifest(folder).get("title") or name.replace("-", " ").replace("_", " ").title()
    module, error = loader.load(name) if not error else (None, error)
    out = {"name": name, "title": _line(title), "public": public, "loaded": module is not None}
    if module is None:
        return {**out, "state": "error", "detail": _line(error), "actions": []}
    on = loader.is_on(name)
    if on:
        ok, detail = _status(ctx, module)
        state = "on" if ok else "error"
    else:
        state, detail = "off", "turned off"
    toggle = {"id": "toggle", "label": "Turn off" if on else "Turn on"}
    return {**out, "state": state, "detail": _line(detail), "actions": [toggle, *(_own_actions(ctx, module) if on else [])]}


def listing(ctx) -> list:
    return [describe(ctx, name) for name in loader.listed()]


def _call(ctx, module, hook, *args):
    """start and stop are the module's; one that fails leaves the list as asked and says so in the module's detail."""
    fn = getattr(module, hook, None)
    if fn is None:
        return None
    try:
        return fn(ctx, *args)
    except Exception as exc:
        return exc


def _with_failure(obj, what, result) -> dict:
    if isinstance(result, Exception):
        return {**obj, "state": "error", "detail": _line(f"{what} failed: {type(result).__name__}: {result}")}
    return obj


def load(ctx, name: str) -> dict:
    folder, _, error = loader.locate(name)
    if error:
        raise UserError(error)
    module, error = loader.load(name)
    if module is None:
        raise UserError(f"{name} {error}")
    names = loader.listed()
    try:
        if name not in names:
            loader.write_list([*names, name])
    except ValueError as exc:
        raise UserError(str(exc))
    loader.set_on(name, True)
    result = _call(ctx, module, "start")
    return _with_failure(describe(ctx, name), "start", result)


def unload(ctx, name: str) -> dict:
    names = loader.listed()
    if name not in names:
        raise UserError(f"{name} is not in the module list")
    before = describe(ctx, name)
    try:
        loader.write_list([n for n in names if n != name])
    except ValueError as exc:
        raise UserError(str(exc))
    module, _ = loader.load(name) if before["loaded"] else (None, None)
    result = _call(ctx, module, "stop") if module is not None and before["state"] != "off" else None
    loader.set_on(name, True)  # a later load starts it on
    gone = {**before, "loaded": False, "state": "off", "detail": "unloaded", "actions": []}
    return _with_failure(gone, "stop", result)


def action(ctx, name: str, action_id: str) -> dict:
    if name not in loader.listed():
        raise UserError(f"{name} is not in the module list")
    module, error = loader.load(name)
    if module is None:
        raise UserError(f"{name} is not loaded: {error}")
    if action_id == "toggle":
        on = not loader.is_on(name)
        loader.set_on(name, on)
        result = _call(ctx, module, "start" if on else "stop")
        return _with_failure(describe(ctx, name), "start" if on else "stop", result)
    if not loader.is_on(name):
        raise UserError(f"{name} is off: turn it on first")
    if action_id not in {a["id"] for a in _own_actions(ctx, module)}:
        raise UserError(f"{name} has no action {action_id!r}")
    result = _call(ctx, module, "action", action_id)  # first, so the object printed is the one after it
    return _with_failure(describe(ctx, name), action_id, result)


def checks(ctx):
    """One doctor line per listed module that won't load: a module the human asked for and isn't running is a problem."""
    out = []
    for name in loader.listed():
        module, error = loader.load(name)
        if module is None:
            out.append(caps.Check(f"module:{name}", False, f"in rex.modules but {error}"))
    return out


def _configure(p):
    p.add_argument("action", nargs="?", choices=["list", "load", "unload", "action"], default="list")
    p.add_argument("name", nargs="?", help="the module, for load, unload and action")
    p.add_argument("id", nargs="?", help="the action's id, for action")
    p.add_argument("--json", action="store_true", help="the menu bar's form (load, unload and action always print JSON)")


def _run(args):
    ctx = caps.CTX
    if args.action == "list":
        rows = listing(ctx)
        if args.json:
            print(json.dumps(rows))
        elif not rows:
            print(f"no modules listed (config rex.modules). Public ones: {', '.join(loader.public_names()) or 'none'}")
        else:
            for row in rows:
                print(f"{row['state']:<5}  {row['name']:<14} {'public ' if row['public'] else 'private'}  {row['detail']}")
        return 0
    if not args.name:
        raise UserError(f"modules {args.action} needs a module name")
    if args.action == "action":
        if not args.id:
            raise UserError("modules action needs a name and an action id")
        out = action(ctx, args.name, args.id)
    else:
        out = (load if args.action == "load" else unload)(ctx, args.name)
    print(json.dumps(out))
    return 1 if out["state"] == "error" else 0


def commands():
    return {"modules": caps.Command(SUMMARY, _configure, _run)}
