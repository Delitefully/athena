"""The board: worker attach columns beside the chief of staff's pane, in the same tab.

`plan` and `ops` are pure. `sync` reads herdr and the ledger, and rebuilds the columns only when the
plan changes and no board column has focus. Columns run `herdr terminal attach <terminal> --takeover`.
"""
import contextlib
import fcntl
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import time

from athena_lib import herdr, ledger, paths, status


def _clamp(r):
    return max(0.1, min(0.9, r))


def plan(width: int, min_col: int, workers: list) -> dict:
    """workers: [{name, urgency, spawned}]. Columns keep spawn order so they do not shuffle."""
    by_spawn = sorted(workers, key=lambda w: (w.get("spawned") or 0, w["name"]))
    names = [w["name"] for w in by_spawn]
    n = len(names)
    hq_ratio = _clamp(min_col / width) if width else 0.5
    if n == 0:
        return {"mode": "empty", "rows": [], "hidden": [], "hq_ratio": 1.0}
    avail = width - min_col
    if avail < min_col:
        return {"mode": "hq-only", "rows": [], "hidden": names, "hq_ratio": 1.0}
    if avail / n >= min_col:
        return {"mode": "columns", "rows": [names], "hidden": [], "hq_ratio": hq_ratio}
    if n > 1 and avail / math.ceil(n / 2) >= min_col:
        top = math.ceil(n / 2)
        return {"mode": "rows", "rows": [names[:top], names[top:]], "hidden": [], "hq_ratio": hq_ratio}
    k = max(1, int(avail // min_col))
    urgent = sorted(by_spawn, key=lambda w: (w.get("urgency", 3), w.get("spawned") or 0))[:k]
    keep = {w["name"] for w in urgent}
    shown = [x for x in names if x in keep]
    return {"mode": "subset", "rows": [shown], "hidden": [x for x in names if x not in keep], "hq_ratio": hq_ratio}


def _chain(start, names, prefix, ops):
    """Split `start` into len(names) equal columns, left to right. Returns refs in order."""
    refs = [start]
    current = start
    for i in range(1, len(names)):
        ref = f"{prefix}{i}"
        ops.append(("split", current, "right", round(1 / (len(names) - i + 1), 4), ref))
        refs.append(ref)
        current = ref
    return refs


def ops(p: dict, hq_pane: str) -> list:
    out = []
    rows = p["rows"]
    if not rows:
        return out
    out.append(("split", hq_pane, "right", round(p["hq_ratio"], 4), "c0"))
    assigned = []
    if len(rows) == 1:
        refs = _chain("c0", rows[0], "c", out)
        assigned = list(zip(refs, rows[0]))
    else:
        out.append(("split", "c0", "down", 0.5, "r1"))
        top = _chain("c0", rows[0], "t", out)
        bottom = _chain("r1", rows[1], "b", out)
        assigned = list(zip(top, rows[0])) + list(zip(bottom, rows[1]))
    for ref, name in assigned:
        out.append(("attach", ref, name))
    return out


def _state_file():
    return paths.ensure() / "board.json"


def load():
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def save(data):
    target = _state_file()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, target)


def hq_pane():
    pane = os.environ.get("ATHENA_HQ_PANE")
    if pane:
        return pane
    try:
        return json.loads((paths.state_dir() / "hq.json").read_text()).get("pane")
    except (OSError, ValueError):
        return None


def _focused(pane_id) -> bool:
    try:
        return bool(herdr.call("pane", "get", pane_id, timeout=10).get("pane", {}).get("focused"))
    except herdr.HerdrError:
        return False


def _exists(pane_id) -> bool:
    try:
        herdr.call("pane", "get", pane_id, timeout=10)
        return True
    except herdr.HerdrError:
        return False


def _close_owned(column) -> bool:
    """Close a column only if it is still ours: herdr may reuse pane ids after a restart."""
    try:
        pane = herdr.call("pane", "get", column["pane"], timeout=10).get("pane", {})
    except herdr.HerdrError:
        return False
    if pane.get("label") != f"board:{column['name']}":
        return False
    try:
        herdr.call("pane", "close", column["pane"], timeout=15)
        return True
    except herdr.HerdrError:
        return False


def _key(p):
    return json.dumps({"mode": p["mode"], "rows": p["rows"], "hq": round(p["hq_ratio"], 2)}, sort_keys=True)


class BoardBusy(Exception):
    pass


@contextlib.contextmanager
def _lock():
    """One board writer at a time: the watcher, spawn and retire may all call sync."""
    f = open(paths.ensure() / "board.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BoardBusy("another board sync is running")
        yield
    finally:
        f.close()


def _hq_owned(pane_id) -> bool:
    """The HQ id may be stale after a herdr restart: only use it while it runs the athena agent."""
    try:
        agent = herdr.call("agent", "get", pane_id, timeout=10).get("agent", {})
    except herdr.HerdrError:
        return False
    return agent.get("name") == paths.hq_name() or agent.get("label") == paths.hq_name()


def sync(force=False) -> dict:
    try:
        with _lock():
            return _sync(force)
    except BoardBusy:
        return {"skipped": "another board sync is running", "changed": False, "postponed": False}


def _sync(force) -> dict:
    hq = hq_pane()
    if not hq:
        raise RuntimeError("no HQ pane: run `athena hq` first, or set ATHENA_HQ_PANE")
    if not os.environ.get("ATHENA_HQ_PANE") and not _hq_owned(hq):
        raise RuntimeError(f"pane {hq} no longer runs the {paths.hq_name()} agent; run `athena hq`")
    layout = herdr.call("pane", "layout", "--pane", hq).get("layout", {})
    width = int(layout.get("area", {}).get("width") or 0)
    live = ledger.live()
    workers = [{"name": w["name"], "spawned": w.get("spawned") or 0, "terminal": w.get("terminal"),
                "urgency": status.urgency(status.quick_state(w))} for w in live if w.get("terminal")]
    p = plan(width, paths.min_col(), workers)
    key = _key(p)
    saved = load()
    columns = saved.get("columns", []) if saved.get("hq") == hq else []
    result = {**p, "width": width, "changed": False, "postponed": False}
    if not force and saved.get("key") == key and saved.get("hq") == hq and all(_exists(c["pane"]) for c in columns):
        return result
    if not force and any(_focused(c["pane"]) for c in columns):
        result["postponed"] = True
        return result
    for c in columns:
        _close_owned(c)
    save({"hq": hq, "key": None, "columns": [], "width": width, "ts": time.time()})
    terminals = {w["name"]: w["terminal"] for w in workers}
    refs, created, new_columns = {}, [], []
    try:
        for op in ops(p, hq):
            if op[0] == "split":
                _, target, direction, ratio, ref = op
                pane_id = herdr.call("pane", "split", refs.get(target, target), "--direction", direction,
                                     "--ratio", str(ratio), "--no-focus")["pane"]["pane_id"]
                refs[ref] = pane_id
                created.append(pane_id)
            else:
                _, ref, name = op
                pane_id = refs[ref]
                herdr.call("pane", "rename", pane_id, f"board:{name}", timeout=15)
                command = " ".join(shlex.quote(a) for a in herdr.argv("terminal", "attach", terminals[name], "--takeover"))
                herdr.call("pane", "run", pane_id, command, timeout=15)
                new_columns.append({"pane": pane_id, "name": name, "terminal": terminals[name]})
    except (herdr.HerdrError, KeyError) as exc:
        for pane_id in created:  # created by this run, so ours even if not yet labelled
            try:
                herdr.call("pane", "close", pane_id, timeout=15)
            except herdr.HerdrError:
                pass
        save({"hq": hq, "key": None, "columns": [], "width": width, "ts": time.time(), "error": str(exc)})
        raise RuntimeError(f"board rebuild failed and was rolled back: {exc}") from exc
    save({"hq": hq, "key": key, "columns": new_columns, "width": width, "ts": time.time()})
    result["changed"] = True
    return result


def clear():
    with contextlib.suppress(BoardBusy):
        with _lock():
            for c in load().get("columns", []):
                _close_owned(c)
            save({})


def watch(interval=2.0):
    last_error = None
    while True:
        try:
            sync()
            last_error = None
        except Exception as exc:  # keep watching; log each distinct error once
            if str(exc) != last_error:
                print(f"board: {exc}", file=sys.stderr, flush=True)
                last_error = str(exc)
        time.sleep(interval)


def _pidfile():
    return paths.ensure() / "board.pid"


def running_pid():
    """The pid in the pidfile, only if that process really is our board watcher."""
    try:
        pid = int(_pidfile().read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return pid if "athena_lib.cli board watch" in cmd else None


def start_watcher():
    pid = running_pid()
    if pid:
        return pid
    log = open(paths.ensure("logs") / "board.log", "a")
    proc = subprocess.Popen([sys.executable, "-m", "athena_lib.cli", "board", "watch"], stdout=log, stderr=log,
                            cwd=str(paths.PLUGIN_ROOT), env=dict(os.environ, PYTHONPATH=str(paths.PLUGIN_ROOT)),
                            start_new_session=True)
    _pidfile().write_text(str(proc.pid))
    return proc.pid


def stop_watcher():
    pid = running_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
    _pidfile().unlink(missing_ok=True)
    return pid
