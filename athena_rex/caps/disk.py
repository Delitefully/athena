"""Named caches: what can be emptied when the disk is short, each regenerable by its own tool, listed with its size. Nothing is emptied unless
named. Which exist depends on the machine, so it looks; `rex.caches` in config adds {name: path}. Worktrees are `workspaces`, which knows what holds work.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import caps, config, lab, world

SUMMARY = "named caches: their sizes, and emptying one by name"
KNOWN = {"go-build": "~/Library/Caches/go-build", "go-mod": "~/go/pkg/mod/cache", "pip": "~/Library/Caches/pip", "uv": "~/.cache/uv", "npm": "~/.npm/_cacache",
         "pnpm": "~/Library/pnpm/store", "yarn": "~/Library/Caches/Yarn", "homebrew": "~/Library/Caches/Homebrew", "cargo": "~/.cargo/registry/cache",
         "playwright": "~/Library/Caches/ms-playwright", "xcode-derived": "~/Library/Developer/Xcode/DerivedData"}
ONLY_LISTED = "Docker's volumes can hold databases, so nothing here prunes them: `docker system df` shows them, and `docker volume prune` is a person's call."


def named() -> dict:
    extra = config.rex("caches")
    return {**KNOWN, **(extra if isinstance(extra, dict) else {})}


def _size_mb(path) -> int:
    try:
        out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=120).stdout.split()
        return int(out[0]) // 1024 if out else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def found() -> list:
    rows = []
    for name, path in sorted(named().items()):
        full = Path(os.path.expanduser(path))
        if full.is_dir():
            rows.append({"name": name, "path": str(full), "mb": _size_mb(full)})
    return sorted(rows, key=lambda r: -r["mb"])


def _may_empty(path) -> bool:
    return world.enabled() or not lab.inside(path, [lab.real_home()])


def empty(name) -> int:
    path = named().get(name)
    if not path:
        raise UserError(f"no cache named {name}: {', '.join(sorted(named()))}")
    full = Path(os.path.expanduser(path))
    if not _may_empty(full):
        raise world.Off(f"{full} is off here: rex.real_world is not set in this state dir's config")
    if full == Path.home() or Path.home() not in full.parents:
        raise UserError(f"{full} isn't a folder under your home, so it isn't emptied")
    size = _size_mb(full) if full.is_dir() else 0
    shutil.rmtree(full, ignore_errors=True)
    return size


def _configure(p):
    p.add_argument("action", nargs="?", choices=["list", "empty"], default="list")
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true")


def _run(args):
    if args.action == "empty":
        if not args.name:
            raise UserError("athena-rex disk empty <name>: which one, by name")
        print(f"{args.name}: {empty(args.name)} MB freed; its tool fills it again when it needs to")
        return 0
    rows = found()
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    free = shutil.disk_usage(os.path.expanduser("~")).free // (1 << 30)
    print(f"{free} GB free")
    for row in rows:
        print(f"{row['mb']:>8} MB  {row['name']:<14} {row['path']}")
    print(ONLY_LISTED)
    return 0


def commands():
    return {"disk": caps.Command(SUMMARY, _configure, _run)}
