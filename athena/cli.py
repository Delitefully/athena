"""`athena <verb>`. Finds the capability that owns the verb; only that one is imported, so a hot command stays fast."""
import os
import sys

from athena.kernel import caps, config, lab, store
from athena.kernel.backend import BackendError

USAGE = "usage: athena <command> [options]  (athena --help lists them)"


def _help() -> int:
    rows = []
    for module in caps.loaded():
        for verb, command in getattr(module, "commands", dict)().items():
            if not command.hidden:
                rows.append((verb, command.help, module.__name__))
    print("athena: a chief of staff for Claude workers, on Rex.\n\nCommands:")
    width = max(len(r[0]) for r in rows) if rows else 0
    for verb, text, _ in sorted(rows):
        print(f"  {verb:<{width}}  {text}")
    print("\nEach command has --help. State dir: ATHENA_STATE_DIR. Architecture: ARCHITECTURE.md.")
    return 0


def version() -> str:
    """This tree's VERSION file: a release's, or a checkout's."""
    try:
        return (store.ROOT / "VERSION").read_text().strip()
    except OSError:
        return "unknown (no VERSION file)"


def _parsed(command, argv):
    import argparse
    parser = argparse.ArgumentParser(prog=f"athena {argv[0]}", description=command.help)
    command.configure(parser)
    return parser.parse_args(argv[1:])


def main(argv=None) -> int:
    if os.environ.get("ATHENA_CANARY"):
        from athena.kernel import canary
        canary.from_environment()
    argv = sys.argv[1:] if argv is None else argv
    hook = bool(argv) and argv[0] == "hook"
    try:
        store.state_dir()
        config.config_file()
    except lab.Refused as exc:
        if hook:
            return 0
        print(f"athena: {exc}", file=sys.stderr)
        return 2
    if argv and argv[0] in ("-h", "--help", "help"):
        return _help()
    if argv[:1] == ["--version"]:
        print(f"athena {version()}")
        return 0
    if not argv:
        argv = ["hq"]  # a bare command opens HQ, as the old athena's did
    command = caps.find_command(argv[0])
    if hook and command is None:
        return 0
    if command is None:
        print(f"athena: no command {argv[0]!r}, or its capability is off in config. {USAGE}", file=sys.stderr)
        return 2
    args = None if hook else _parsed(command, argv)
    try:
        return command.run(args) or 0
    except lab.Refused as exc:
        print(f"athena: {exc}", file=sys.stderr)
        return 2
    except UserError as exc:
        print(f"athena: {exc}", file=sys.stderr)
        return 1
    except BackendError as exc:
        print(f"athena: the backend said {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except OSError as exc:
        if exc.errno not in store.NO_ROOM:
            raise
        print(f"athena: the disk is full, so this stopped where it was: {exc}", file=sys.stderr)
        return 1


class UserError(Exception):
    pass
