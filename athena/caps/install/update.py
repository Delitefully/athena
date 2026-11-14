"""`athena update`: the newest release from the repo, beside the running one, switched in one rename, kept only if it is healthy.

1. Fetch the repo into <root>/source, a clone of its own.
2. Pick: on the "release" channel the highest vX.Y.Z tag above the running version; on "main", main's head when it isn't what runs.
3. Make the release from that commit, beside the running one. Nothing running has changed yet.
4. Ask the running release's doctor which checks pass.
5. Record the switch, swap `current`, restart athena's own launchd jobs.
6. Ask the new release's doctor until it agrees or the wait is over. A check that passed before and fails now, or a doctor that
   can't answer, swaps back and restarts the jobs again.

An update killed between 5 and 6 leaves its switch on record. The next update, install or rollback, or doctor's `update` check
once no update holds the lock, rolls it back to the release that was running: only a release that passed its health check
stays, unless the one it came from was removed by hand, when what the link names is kept and the record says so.
"""
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time

from athena.cli import UserError
from athena.caps.install import releases
from athena.kernel import caps, lab, store, world

GIT_TIMEOUT = 120
CHECK_EVERY = 24 * 3600
_TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")


class Interrupted(BaseException):
    """SIGTERM during the switch, raised where the update stands so the switch is undone like Ctrl-C's."""


def _wait() -> float:
    return float(os.environ.get("ATHENA_UPDATE_WAIT") or 60)


def _git(*args, timeout=GIT_TIMEOUT, binary=False):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=not binary, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UserError(f"git {args[0]} did not run: {exc}")
    if done.returncode:
        err = done.stderr if not binary else done.stderr.decode(errors="replace")
        raise UserError(f"git {' '.join(args[:2])} failed: {err.strip()[:300]}")
    return done.stdout


def _source(record) -> str:
    """A clone of the repo, kept between updates so each fetch brings only what is new."""
    clone = releases.root() / "source"
    if not (clone / ".git").is_dir():
        shutil.rmtree(clone, ignore_errors=True)
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--quiet", "--no-checkout", record["repo"], str(clone))
    else:
        _git("-C", str(clone), "remote", "set-url", "origin", record["repo"])
        _git("-C", str(clone), "fetch", "--quiet", "--prune", "--prune-tags", "--tags", "origin")
    return str(clone)


def _newest_tag(names) -> tuple:
    """(version, tag) of the highest vX.Y.Z among `names`, or (None, None)."""
    found = sorted(((releases.version_key(m.group(1)), m.group(1), name) for name in names for m in [_TAG.match(name)] if m), reverse=True)
    return (found[0][1], found[0][2]) if found else (None, None)


def _target(clone, record) -> dict:
    """{"version", "ref", "commit"} of what the channel offers now."""
    if record["channel"] == "main":
        commit = _git("-C", clone, "rev-parse", "refs/remotes/origin/main^{commit}").strip()
        version = _git("-C", clone, "show", f"{commit}:VERSION").strip()
        return {"version": version, "ref": "main", "commit": commit}
    version, tag = _newest_tag(_git("-C", clone, "tag", "-l", "v*").split())
    if not tag:
        raise UserError(f"no release (a vX.Y.Z tag) is published on {record['repo']} yet: `athena update --channel main` follows main")
    commit = _git("-C", clone, "rev-parse", f"refs/tags/{tag}^{{commit}}").strip()
    return {"version": version, "ref": tag, "commit": commit}


def _newer(target, current) -> bool:
    """Whether `target` is something to install over `current`. Nothing found (no tag yet) is never newer, nor an older version.
    On main, a commit is newer only when what runs is behind it on the same line and its VERSION is no lower (_main_ahead)."""
    if target.get("ref") == "main":
        return _main_ahead(target.get("commit"), current) == "ahead"
    if not target.get("version"):
        return False
    return not current.get("version") or releases.version_key(target["version"]) > releases.version_key(current["version"])


MAIN_SAYS = {
    "unfetched": "main is at {commit}, not fetched yet: `athena update --check` compares",
    "off-line": "this runs commits main doesn't have, so main is never offered as an update",
    "no-commit": "no commit on record for what runs, so main can't be compared: install from a clean checkout",
}


def _main_ahead(commit, current) -> str:
    """How main's head `commit` stands to what runs, from the update clone: "ahead" (what runs is its ancestor, and its VERSION
    is no lower), "not" (the same, behind, another line, or a lower version), or why the clone can't tell (a MAIN_SAYS key).
    A Mac ahead of the public main, or on another line, is never offered main."""
    if not commit or commit == current.get("commit"):
        return "not"
    if not current.get("commit"):
        return "no-commit"
    clone = releases.root() / "source"

    def git_ok(*args):
        try:
            return subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, timeout=30,
                                  stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    if not (clone / ".git").is_dir() or not git_ok("cat-file", "-e", f"{commit}^{{commit}}"):
        return "unfetched"
    if not git_ok("cat-file", "-e", f"{current['commit']}^{{commit}}"):
        return "off-line"
    if not git_ok("merge-base", "--is-ancestor", current["commit"], commit):
        return "not"
    try:
        version = subprocess.run(["git", "-C", str(clone), "show", f"{commit}:VERSION"], capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "not"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return "not"
    return "ahead" if not current.get("version") or releases.version_key(version) >= releases.version_key(current["version"]) else "not"


def _export(clone, commit, into):
    """The tree at `commit`, and nothing else: no .git, no file outside `into`."""
    data = _git("-C", clone, "archive", "--format=tar", commit, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name.split("/") or not (member.isfile() or member.isdir() or member.issym()):
                raise UserError(f"the release's archive has an unsafe entry: {member.name}")
        tar.extractall(into)


def doctor():
    """{check: ok} from the running release's doctor, or None when it can't answer."""
    deadline = time.monotonic() + 30
    while True:
        try:
            health = json.loads(releases.installed_command("doctor", "--json").stdout)
        except ValueError:
            return None
        if not health.get("busy"):
            checks = health.get("checks")
            if not isinstance(checks, dict) or not checks:
                return None
            return {name: bool(row.get("ok")) for name, row in checks.items() if isinstance(row, dict)}
        if time.monotonic() > deadline:
            return None
        time.sleep(1)


def _verdict(before) -> str:
    """"" when no check the old release passed fails on the new one, else why not. A check the new release no longer has is
    no failure: a release may drop one. Asked until the wait is over, as jobs just restarted take a moment to come up."""
    deadline = time.monotonic() + _wait()
    while True:
        after = doctor()
        lost = sorted(name for name, ok in before.items() if ok and after.get(name) is False) if after is not None else None
        if lost == []:
            return ""
        if time.monotonic() >= deadline:
            return "its doctor did not answer" if lost is None else f"it fails what passed before: {', '.join(lost)}"
        time.sleep(2)


def _back(record, why, jobs, out=sys.stdout) -> dict:
    """Undo a switch on record: the release it came from runs again. Said on `out`: doctor's own output is its JSON."""
    switch = record["switch"]
    releases.point(switch["from"])
    record = {**record, "current": switch["from"], "switch": None, "last_update": {"result": "rolled back", "detail": why, "ts": time.time()}}
    releases.save(record)  # before the restart, which ends a doctor job that is running this repair
    print(f"  ~ back to {switch['from']['version'] or switch['from']['release']}: {why}", file=out)
    print(f"  · {releases.restart_services(jobs)}", file=out)
    return record


def recover(record, out=sys.stdout) -> dict:
    """A switch left on record by an update that was killed before its verdict."""
    if not record["switch"]:
        return record
    jobs = releases.installed_jobs()  # before `current` moves
    if not releases.exists(record["switch"]["from"]):  # removed by hand: nothing to go back to, so what the link names stays
        kept = next((e for e in (record["switch"]["to"], record["current"]) if e and e["release"] == releases.pointed()), record["current"])
        record = {**record, "current": kept, "switch": None,
                  "last_update": {"result": "kept", "detail": "the release it came from is gone", "ts": time.time()}}
        releases.save(record)
        print(f"  = {kept['release']} stays: the release the cut-off update came from is gone", file=out)
        print(f"  · {releases.restart_services(jobs)}", file=out)  # the jobs may still run what the killed update left them on
        return record
    return _back(record, f"the update to {record['switch']['to']['version']} was cut off before its health check", jobs, out)


def _switch(record, made, before, jobs) -> int:
    old = record["current"]
    record = {**record, "switch": {"from": old, "to": made, "since": time.time()}}
    releases.save(record)
    previous_handler = signal.signal(signal.SIGTERM, _on_term)
    try:
        releases.point(made)
        print(f"  ~ release {made['release']}, was {old['release']}")
        print(f"  · {releases.restart_services(jobs)}")
        why = _verdict(before)
    except BaseException:
        _back(record, "the update was interrupted", jobs)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    if why:
        _back(record, f"{made['version']} failed its health check: {why}", jobs)
        return 1
    releases.save({**record, "current": made, "previous": old, "switch": None, "source": None,
                   "last_update": {"result": "updated", "detail": f"{old['version']} to {made['version']}", "ts": time.time()}})
    releases.prune(made, old)
    print(f"\nathena {made['version']} runs, and passes what {old['version']} passed. `athena install --rollback` goes back.")
    return 0


def _on_term(signum, frame):
    raise Interrupted()


def update(check_only=False, channel=None, repo=None) -> int:
    from athena.caps import install
    install.refuse_in_the_lab("update")
    with releases.locked() as got:
        if not got:
            raise UserError("another update is running")
        record = recover(releases.load())  # first: while a switch is on record, `current` is what it came from
        if not record["current"] or not releases.exists(record["current"]):
            raise UserError("athena is not installed here: run install from a checkout first (<checkout>/bin/athena install)")
        if channel or repo:
            record = {**record, "channel": channel or record["channel"], "repo": repo or record["repo"]}
            releases.save(record)
        clone = _source(record)
        target = _target(clone, record)
        record = {**record, "latest": {**target, "checked": time.time()}}
        releases.save(record)
        current = record["current"]
        if not _newer(target, current):
            stands = _main_ahead(target["commit"], current) if target["ref"] == "main" else "not"
            print(f"athena {current['version']} is the latest on {record['channel']} ({target['ref']})" if stands not in MAIN_SAYS
                  else f"athena {current['version']}: nothing to install; " + MAIN_SAYS[stands].format(commit=target["commit"][:10]))
            return 0
        if check_only:
            print(f"athena {target['version']} ({target['ref']}) is out; this is {current['version']}. `athena update` installs it")
            return 0
        print(f"athena update: {current['version']} to {target['version']} ({target['ref']}, {target['commit'][:10]})\n")
        staging = releases.root() / f".staging.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            _export(clone, target["commit"], staging)
            made = {**releases.make(staging, record["lab_guard"] == "out"), "commit": target["commit"]}
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if made["release"] == current["release"]:
            releases.save({**record, "current": {**current, "commit": target["commit"]}})
            print(f"athena {current['version']} is the latest on {record['channel']}: {target['ref']} holds the same files")
            return 0
        before = doctor()
        if before is None:
            raise UserError("the running release's doctor did not answer, so there is nothing to hold the new one to: nothing changed")
        try:
            return _switch(record, made, before, releases.installed_jobs())
        except Interrupted:
            return 128 + signal.SIGTERM


def _latest_line(record) -> str:
    latest, current = record["latest"], record["current"]
    if not latest or "ref" not in latest:  # never asked, or never answered
        return f"athena {current['version']}"
    on_main = latest.get("ref") == "main"
    if on_main:
        stands = _main_ahead(latest.get("commit"), current)
        if stands == "ahead":
            return f"athena {current['version']}; main at {latest['commit'][:10]} is out: `athena update`"
        if stands in MAIN_SAYS:
            return f"athena {current['version']}; " + MAIN_SAYS[stands].format(commit=str(latest.get("commit"))[:10])
    elif _newer(latest, current):
        return f"athena {current['version']}; {latest['version']} is out: `athena update`"
    if not on_main and not latest.get("version"):
        return f"athena {current['version']}; no release is published yet"
    return f"athena {current['version']}, the latest on {record['channel']}"


def _refresh(record) -> dict:
    """What the repo offers, asked at most once a day and only with the door open: one ls-remote, nothing fetched. A failed ask
    counts as asked, so an offline Mac waits a day too. Written under the lock, onto the record as it is then."""
    latest = record["latest"] or {}
    if not world.enabled() or time.time() - float(latest.get("checked") or 0) < CHECK_EVERY:
        return record
    try:
        refs = _git("ls-remote", "--tags", "--heads", record["repo"], timeout=10)
    except UserError:
        found = latest
    else:
        pairs = [line.split("\t") for line in refs.splitlines() if "\t" in line]
        if record["channel"] == "main":
            found = {"version": None, "ref": "main", "commit": next((sha for sha, ref in pairs if ref == "refs/heads/main"), None)}
        else:
            version, tag = _newest_tag(ref[len("refs/tags/"):] for _, ref in pairs if ref.startswith("refs/tags/") and not ref.endswith("^{}"))
            found = {"version": version, "ref": tag, "commit": None}
    with releases.locked() as got:
        if not got:  # an update is writing the record: what it finds is newer than this
            return record
        record = {**releases.load(), "latest": {**found, "checked": time.time()}}
        releases.save(record)
    return record


def check(ctx):
    """Doctor's `update` row: a switch nobody is finishing is rolled back by its repair; otherwise, which version runs and
    whether a newer one is out, which never fails doctor."""
    if lab.active() and not lab.scratch_place(releases.home()):  # a lab doctor on the real home never touches its install
        return []
    record = releases.load()
    if not record["current"]:
        return []
    if record["switch"]:
        with releases.locked() as got:
            if not got:
                return [caps.Check("update", True, "an update is under way")]
        return [caps.Check("update", False, f"an update to {record['switch']['to']['version']} was cut off before its health check",
                           repair=lambda: _repair_switch(), grace=0)]
    return [caps.Check("update", True, _latest_line(_refresh(record)))]


def _repair_switch():
    with releases.locked() as got:
        if got:
            recover(releases.load(), out=sys.stderr)
