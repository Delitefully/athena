"""See the effective config and which capabilities are on."""
import json

from athena_rex.kernel import caps, config

SUMMARY = "the effective config, and which modules are on"


def _configure(p):
    p.add_argument("action", nargs="?", choices=["show", "path", "modules"], default="show")


def _run(args):
    if args.action == "path":
        print(config.config_file())
    elif args.action == "modules":
        on = set(caps.enabled())
        for name in caps.found():
            print(f"{'on ' if name in on else 'off'}  {name:<12} {caps.load(name).SUMMARY if name in on else ''}")
    else:
        rows = {key: {"value": str(config.get(key)), "from": config.origin(key)} for key in config.DEFAULTS}
        rows["rex"] = {key: config.rex(key) for key in config.REX_DEFAULTS}
        print(json.dumps(rows, indent=1, default=str))
    return 0


def commands():
    return {"config": caps.Command(SUMMARY, _configure, _run)}
