"""The chief of staff's own space: create it once, then focus it, and restart its Claude in place."""
import json
import os
import shutil
import subprocess
import sys
import time

from athena_lib import board, dash, herdr, paths, retire


def _file():
    return paths.ensure() / "hq.json"


def load():
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _alive(pane):
    try:
        agent = herdr.call("agent", "get", pane, timeout=10).get("agent", {})
        return agent.get("name") == paths.hq_name()
    except herdr.HerdrError:
        return False


def _claude_args():
    settings = paths.ensure("settings") / "hq.json"
    settings.write_text(json.dumps({"crossSessionInbound": "accept",
                                    "env": {"ATHENA_ROLE": "hq", "ATHENA_STATE_DIR": str(paths.state_dir())}}, indent=2))
    args = ["--name", paths.hq_name(), "--append-system-prompt-file", str(paths.PLUGIN_ROOT / "hq.md"),
            "--settings", str(settings)]
    if os.environ.get("ATHENA_PLUGIN_DIR"):
        args += ["--plugin-dir", os.environ["ATHENA_PLUGIN_DIR"]]
    return args


def _start_agent(pane, *extra):
    herdr.call("agent", "start", paths.hq_name(), "--kind", "claude", "--pane", pane, "--timeout", "120000",
               "--", *extra, *_claude_args(), timeout=200)


def _session():
    """The id of the conversation HQ's Claude has open, as herdr sees it."""
    try:
        agent = herdr.call("agent", "get", paths.hq_name(), timeout=10).get("agent", {})
    except herdr.HerdrError:
        return None
    found = agent.get("agent_session") or {}
    if agent.get("name") != paths.hq_name() or found.get("kind") != "id":
        return None
    return found.get("value") or None


def _remember_session(record):
    """Keep the conversation id in hq.json, so a restart can resume it even after HQ's Claude has gone."""
    session = _session()
    if session and session != record.get("session"):
        record["session"] = session
        _file().write_text(json.dumps(record, indent=2))
    return record


LABEL = "athena"


def keep_label(record=None, owned=False):
    """Name the hq space and its Claude pane `athena`, and keep them so.

    The renamer plugin retitles a space from its first prompt, and an unlabelled pane shows
    its agent kind (`claude`) on its border. `owned` skips the check that the pane still runs
    the athena agent, for a pane this call's caller just created.
    """
    record = record if record is not None else load()
    ws, pane = record.get("workspace"), record.get("pane")
    if not ws or not pane or not (owned or _alive(pane)):
        return
    try:
        if herdr.call("workspace", "get", ws, timeout=10).get("workspace", {}).get("label") != LABEL:
            herdr.call("workspace", "rename", ws, LABEL, timeout=10)
        if herdr.call("pane", "get", pane, timeout=10).get("pane", {}).get("label") != LABEL:
            herdr.call("pane", "rename", pane, LABEL, timeout=10)
    except herdr.HerdrError:
        pass


def _workspace_is_hq(record) -> bool:
    try:
        ws = herdr.call("workspace", "get", record["workspace"], timeout=10).get("workspace", {})
    except (herdr.HerdrError, KeyError):
        return False
    return ws.get("label") == LABEL


def _routine():
    herdr.call("agent", "prompt", paths.hq_name(),
               "Start your HQ routine: run `athena status`, start `athena watch` under Monitor, then tell me what is live and wait.")


def _helpers(start_board, start_dash):
    """The board watcher and the dashboard, once HQ's Claude runs. Neither may stop HQ from opening."""
    if start_board:
        board.start_watcher()
    if start_dash:
        try:
            dash.start()
        except Exception as exc:
            print(f"dashboard not started: {exc}", file=sys.stderr)


def hq(cwd=None, focus=True, start_board=True, start_dash=None) -> dict:
    start_dash = start_board if start_dash is None else start_dash
    current = load()
    if current.get("pane") and _alive(current["pane"]):
        keep_label(current, owned=True)
        if focus and current.get("workspace"):
            herdr.call("workspace", "focus", current["workspace"])
        _helpers(start_board, start_dash)
        return {**current, "created": False}
    if current.get("pane") and _workspace_is_hq(current):
        # The hq space survived but its Claude did not (exited, or a herdr restart): restart it there.
        _start_agent(current["pane"])
        _remember_session(current)
        keep_label(current, owned=True)
        _routine()
        if focus:
            herdr.call("workspace", "focus", current["workspace"])
        _helpers(start_board, start_dash)
        return {**current, "created": False, "restarted": True}
    cwd = os.path.realpath(os.path.expanduser(cwd or os.environ.get("ATHENA_HQ_CWD") or "~/Developer"))
    claim = paths.claim_file(cwd)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("hq\n")
    try:
        created = herdr.call("workspace", "create", "--cwd", cwd, "--label", LABEL, "--focus" if focus else "--no-focus")
        pane = created["root_pane"]["pane_id"]
        _start_agent(pane)
    finally:
        claim.unlink(missing_ok=True)
    record = {"pane": pane, "workspace": created["workspace"]["workspace_id"],
              "terminal": created["root_pane"].get("terminal_id"), "cwd": cwd}
    _file().write_text(json.dumps(record, indent=2))
    _remember_session(record)
    keep_label(record, owned=True)
    _routine()
    _helpers(start_board, start_dash)
    return {**record, "created": True}


RESTART_ROUTINE = ("HQ restarted: run your session-start routine (athena status --pr, re-arm athena watch under Monitor, "
                   "re-arm any stacked-PR watch you were running per standing-orders.md, check needs-you.md), "
                   "then tell me what changed.")


class RestartError(Exception):
    pass


def lock_file():
    return paths.ensure() / "hq-restart.lock"


def _holder():
    """The pid of a restart still running, if any."""
    try:
        pid = int(lock_file().read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def _lock():
    for _ in range(2):
        try:
            fd = os.open(lock_file(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid = _holder()
            if pid:
                raise RestartError(f"an HQ restart is already running (pid {pid}); see {_log_file()}")
            lock_file().unlink(missing_ok=True)  # its restart died without cleaning up
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return
    raise RestartError(f"could not take {lock_file()}")


def _unlock():
    try:
        if lock_file().read_text().strip() == str(os.getpid()):
            lock_file().unlink()
    except OSError:
        pass


def _log_file():
    return paths.ensure("logs") / "hq-restart.log"


def _say(text):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, file=sys.stderr, flush=True)


def _notify(title, body):
    try:
        herdr.call("notification", "show", title, "--body", body, "--sound", "request", timeout=10)
    except herdr.HerdrError:
        pass


def _claude_bin():
    """herdr's key bindings may run athena with a short PATH."""
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def update_claude():
    """`claude update`, then the marketplaces and the user-scope plugins. A failure is a warning, never a stop.

    Project-scope installs belong to other checkouts (often live workers'), so they are left alone. No `--yes`:
    a plugin whose marketplace changed its install command waits for a person to accept it by hand.
    """
    claude = _claude_bin()
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    warnings = []

    def run(*args, timeout=300):
        try:
            proc = subprocess.run([claude, *args], capture_output=True, text=True, timeout=timeout, env=env,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            proc = None
            detail = str(exc)
        else:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            if proc.returncode == 0:
                _say(f"claude {' '.join(args)}: {(proc.stdout or '').strip()[-200:] or 'ok'}")
                return proc
        warnings.append(f"claude {' '.join(args)} failed: {detail}")
        _say("warning: " + warnings[-1])
        return None

    run("update")
    run("plugin", "marketplace", "update")
    listed = run("plugin", "list", "--json", timeout=60)
    try:
        plugins = json.loads(listed.stdout) if listed else []
    except ValueError:
        plugins = []
    seen = []
    for p in plugins:
        if isinstance(p, dict) and p.get("scope") == "user" and p.get("id") and p["id"] not in seen:
            seen.append(p["id"])
            run("plugin", "update", p["id"], "--scope", "user", timeout=120)
    return warnings


def _wait_idle():
    """When HQ runs the restart about itself it is still finishing that turn: /exit typed now would be queued."""
    wait = int(float(os.environ.get("ATHENA_HQ_IDLE_WAIT") or 300) * 1000)
    try:
        herdr.call("agent", "wait", paths.hq_name(), "--until", "idle", "--until", "done", "--timeout", wait,
                   timeout=wait / 1000 + 15)
        return True
    except herdr.HerdrError:
        return False


def restart_now(update=True) -> dict:
    """The restart itself, in the detached child: update, exit HQ's Claude, resume its conversation in the same pane."""
    _lock()
    try:
        return _restart(update)
    finally:
        _unlock()


def _restart(update):
    record = load()
    name, pane = paths.hq_name(), record.get("pane")
    if not pane:
        raise RestartError("there is no HQ to restart: start one with `athena hq`")
    if update:
        update_claude()
    session = _session() or record.get("session")
    if retire.agent_owned(name):
        if not _wait_idle():
            _say("HQ did not go idle; sending /exit anyway")
        grace = float(os.environ.get("ATHENA_EXIT_GRACE") or 20)
        if not retire.exit_claude(name, grace):
            msg = f"HQ's Claude ({name} in pane {pane}) did not exit; it is still running and nothing was restarted"
            _say("FAILED: " + msg)
            _notify("athena: HQ restart failed", msg)
            raise RestartError(msg)
        _say(f"HQ's Claude exited (conversation {session or 'unknown'})")
    else:
        _say(f"HQ's Claude was not running; resuming conversation {session or 'unknown'}")
    time.sleep(1)  # let the pane's shell draw its prompt
    if session:
        try:
            _start_agent(pane, "--resume", session)
        except herdr.HerdrError as exc:
            if not retire.agent_owned(name):
                return _fallback(f"resuming conversation {session} failed ({exc})")
            _say(f"herdr gave up waiting ({exc}), but the resumed Claude is up")
    else:
        return _fallback("no conversation id was known for HQ")
    _remember_session(load())
    keep_label(owned=True)
    herdr.call("agent", "prompt", name, RESTART_ROUTINE)
    _say(f"HQ resumed conversation {session} in pane {pane}")
    return {"resumed": True, "session": session, "pane": pane}


def _fallback(why):
    msg = f"{why}: starting a fresh HQ conversation instead. The old one is still there: claude --resume <id>"
    _say("FALLBACK: " + msg)
    _notify("athena: HQ restarted without its conversation", msg)
    result = hq(focus=False, start_board=False, start_dash=False)
    _say(f"fresh HQ started in pane {result.get('pane')}")
    return {**result, "fallback": True, "reason": why}


def restart(update=True) -> dict:
    """Start the restart detached and return at once: when HQ runs this about itself, exiting HQ must not kill it."""
    pid = _holder()
    if pid:
        raise RestartError(f"an HQ restart is already running (pid {pid}); see {_log_file()}")
    log = _log_file()
    argv = [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "athena"), "hq", "restart", "--child"]
    if not update:
        argv.append("--no-update")
    with open(log, "a") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                cwd=str(paths.PLUGIN_ROOT))
    return {"pid": proc.pid, "log": str(log), "message": f"restarting HQ; log at {log}"}
