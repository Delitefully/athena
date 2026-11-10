"""The menu bar app, Knot in the status bar: its source is menubar/ in the repo, built by menubar/build.sh with swiftc.

Its login job runs `athena-rex menubar run`, which hands over to the built app and tells it which athena-rex to call. The app
reads `services status --json`, `status --json` and `modules --json`, and runs everything it does through this CLI, so it
never names launchctl or the Rex server itself and the lab's doors hold for it as for any command.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from athena_rex.cli import UserError
from athena_rex.kernel import caps, store

SUMMARY = "the menu bar app: each service, the fleet and each module, with Quit athena and Start athena"


SOURCE = store.ROOT / "menubar"
STAMP = "source.sha256"


def app() -> Path:
    return Path(os.environ.get("ATHENA_MENUBAR_APP") or SOURCE / "build" / "AthenaRexMenu.app")


def binary() -> Path:
    return app() / "Contents" / "MacOS" / "AthenaRexMenu"


def _sources() -> list:
    return sorted([*(SOURCE / "Sources").glob("*.swift"), *(SOURCE / "glyphs").glob("*.svg"), SOURCE / "build.sh"])


def source_digest() -> str:
    """By content, not time: a release is a copy, and a copy's times say nothing about which is newer."""
    digest = hashlib.sha256()
    for path in _sources():
        digest.update(str(path.relative_to(SOURCE)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _stamp() -> Path:
    return app() / "Contents" / "Resources" / STAMP


def stale() -> bool:
    try:
        return not binary().is_file() or _stamp().read_text().strip() != source_digest()
    except OSError:
        return True


def ensure_built(dry_run=False):
    """`services install` asks this first: the app built by menubar/build.sh when it is missing or its sources changed.
    One named by ATHENA_MENUBAR_APP is someone else's to build, and must be there. Returns what it did, or None."""
    if os.environ.get("ATHENA_MENUBAR_APP"):
        if not binary().is_file():
            raise UserError(f"ATHENA_MENUBAR_APP names {app()}, which has no {binary().name} in it")
        return None
    if not stale():
        return None
    if dry_run:
        return f"build {app()} with {SOURCE / 'build.sh'}"
    try:
        done = subprocess.run([str(SOURCE / "build.sh")], capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UserError(f"the menu bar app did not build: {exc}")
    if done.returncode != 0:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise UserError(f"the menu bar app did not build ({done.returncode}): {said[-1] if said else 'no output'}; nothing was installed")
    _stamp().write_text(source_digest() + "\n")
    return f"built {app()}"


def _configure(p):
    p.add_argument("action", choices=["run", "path"])


def _run(args):
    if args.action == "path":
        print(app())
        return 0
    if not binary().is_file():
        # a clean exit, so launchd doesn't try again every ten seconds for a build that isn't there
        print(f"the menu bar app is not built at {app()}: run {store.ROOT / 'menubar' / 'build.sh'}", file=sys.stderr)
        return 0
    env = {**os.environ, "ATHENA_REX_CLI": str(store.ROOT / "bin" / "athena-rex")}
    os.execve(str(binary()), [str(binary())], env)


def services(ctx):
    return [caps.Service("menubar", ["menubar", "run"], interactive=True)]


def commands():
    return {"menubar": caps.Command(SUMMARY, _configure, _run)}
