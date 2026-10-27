"""The chief of staff's own space: create it once, then focus it, and restart its Claude in place."""
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from athena_lib import board, composer, config, dash, handoff, herdr, paths, retire


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


def _claude_args(write=True):
    settings = (paths.ensure("settings") if write else paths.state_dir() / "settings") / "hq.json"
    # FORCE_HYPERLINK: Claude Code draws OSC 8 links only for terminals it knows, and herdr is not one, though it
    # renders them; without it every link in HQ (the status band's "open dash →" and #n among them) shows its raw URL.
    # remoteControlAtStartup: HQ is the one session on Remote Control, even if the user setting goes (workers: off).
    if write:
        data = {"crossSessionInbound": "accept", "remoteControlAtStartup": True,
                "env": {"ATHENA_ROLE": "hq", "ATHENA_HQ": paths.hq_name(), "ATHENA_STATE_DIR": str(paths.state_dir()),
                        "FORCE_HYPERLINK": "1"}}
        allow = _hq_allow()
        if allow:
            data["permissions"] = {"allow": allow}
        settings.write_text(json.dumps(data, indent=2))
    args = ["--name", paths.hq_name(), "--append-system-prompt-file", str(paths.PLUGIN_ROOT / "hq.md"),
            "--settings", str(settings)]
    if os.environ.get("ATHENA_PLUGIN_DIR"):
        args += ["--plugin-dir", os.environ["ATHENA_PLUGIN_DIR"]]
    return args


def _hq_allow() -> list:
    """config hq_allow: permission rules for HQ's settings alone (never a worker's)."""
    try:
        rules = config.get("hq_allow") or []
    except config.ConfigError:
        return []
    return [r for r in rules if isinstance(r, str) and r.strip()] if isinstance(rules, list) else []


def _start_argv(pane, *extra, write=True):
    """The herdr argv that starts HQ's Claude in `pane`. `write=False` (a dry run) leaves the settings file alone."""
    return ["agent", "start", paths.hq_name(), "--kind", "claude", "--pane", pane, "--timeout", "120000",
            "--", *extra, *_claude_args(write)]


def _start_agent(pane, *extra):
    herdr.call(*_start_argv(pane, *extra), timeout=200)


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
    """Keep the conversation id in hq.json, so a restart can resume it even after HQ's Claude has gone, and give
    herdr HQ's whole resume command: else a herdr restart brings HQ back as a bare `claude --resume`, without
    ATHENA_ROLE or hq.md. hq.json keeps the args for the SessionStart hook, which registers each new session."""
    session = _session()
    args = _claude_args()
    if session and (session != record.get("session") or args != record.get("claude_args")):
        record.update(session=session, claude_args=args)
        _file().write_text(json.dumps(record, indent=2))
    _register_resume(record.get("pane"), session, args)
    return record


def _register_resume(pane, session, args):
    if not pane or not session:
        return
    try:
        herdr.register_resume(pane, session, args)
    except herdr.HerdrError:
        pass


def _hq_start() -> dict:
    """hq-start.json: the session athena's SessionStart hook last saw HQ's own Claude start (written only for HQ's own
    sessions, after the hook's nested-session check), with its source and time."""
    try:
        data = json.loads((paths.state_dir() / "hq-start.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _vouch(session, source):
    """Record `session` in hq-start.json as HQ's own conversation, as athena's SessionStart hook does."""
    target = paths.state_dir() / "hq-start.json"
    tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps({"source": source, "session_id": session, "ts": time.time()}))
    os.replace(tmp, target)


def _forget_session(old):
    """A rotation is about to start a fresh HQ: hq.json and hq-start.json stop naming the old conversation."""
    record = load()
    record["previous_session"], record["session"] = old, None
    _file().write_text(json.dumps(record, indent=2))
    (paths.state_dir() / "hq-start.json").unlink(missing_ok=True)


def _restore_session(old):
    """herdr says the fresh HQ did not start: the old conversation is HQ's again, to resume, unless athena's hook
    already saw the fresh one start (hq-start.json names another id, which herdr was told to resume): then that one
    stays HQ's, and hq.json names none, so `own_session` reads it from hq-start.json. Returns the id HQ resumes on."""
    vouched = _hq_start().get("session_id")
    if vouched and vouched != old:
        return vouched
    record = load()
    record["session"] = old
    _file().write_text(json.dumps(record, indent=2))
    if old:
        _vouch(old, "resume")
    return old


def own_session(record=None):
    """The conversation to resume HQ on: hq.json's, unless the hook saw HQ's own Claude start another since hq.json
    was last written (a /clear no doctor tick has recorded yet), then that one."""
    record = load() if record is None else record
    start = _hq_start()
    try:
        written = _file().stat().st_mtime
    except OSError:
        written = 0
    try:
        newer = start.get("session_id") and float(start.get("ts") or 0) > written
    except (TypeError, ValueError):
        newer = False
    # hq.json naming none (just after a rotation whose new id herdr had not reported yet): the hook's is HQ's, since
    # the rotation cleared hq-start.json before HQ started.
    return start["session_id"] if newer or (start.get("session_id") and not record.get("session")) else \
        record.get("session")


def trusted_session(record=None):
    """HQ's conversation: the one herdr reports for HQ's pane when athena's hook saw HQ's own Claude start it, else
    `own_session`. herdr's own hook has no nested-session check, so a `claude -p --resume X` (or a nested compaction)
    run from HQ's Bash can make herdr report X for HQ's pane: that id alone is never trusted."""
    live = _session()
    if live and live == _hq_start().get("session_id"):
        return live
    return own_session(record)


def sync_session(session) -> bool:
    """Record `session`, the conversation herdr says HQ's Claude has open, as HQ's in hq.json, and give herdr its
    resume command for it, when hq.json holds another: after a /clear the id changes while HQ runs, and a later
    resume (doctor's, or herdr's after a restart) must bring back the conversation HQ was on. Only an id athena's
    hook saw HQ's own Claude start (hq-start.json) is recorded: anything else herdr reports may be a nested session's.
    True when it changed."""
    record = load()
    if not session or not record or session == record.get("session"):
        return False
    if session != _hq_start().get("session_id"):
        return False
    record["session"] = session
    _file().write_text(json.dumps(record, indent=2))
    _register_resume(record.get("pane"), session, record.get("claude_args") or _claude_args(write=False))
    return True


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


def _routine(text=None):
    """Prompt HQ's start routine (a fresh HQ's, or `text`), and note when in hq.json: doctor sends the resumed
    routine only to an HQ that started after the last one went out."""
    herdr.call("agent", "prompt", paths.hq_name(), text or
               "Start your HQ routine: run `athena status`, check the athena-watch mod runs `athena watch` (Monitor only as the "
               "skill's fallback), then tell me what is live and wait.")
    record = load()
    if record:
        record["routine_at"] = time.time()
        _file().write_text(json.dumps(record, indent=2))


def _helpers(start_board, start_dash):
    """The board watcher and the dashboard, once HQ's Claude runs. Neither may stop HQ from opening."""
    if start_board:
        board.start_watcher()
    if start_dash:
        try:
            dash.start(hq=0)  # runs until stopped: no longer tied to HQ's process
        except Exception as exc:
            print(f"dashboard not started: {exc}", file=sys.stderr)


class ResumeError(Exception):
    """HQ's conversation could not be resumed. athena never starts a fresh one in its place on its own."""


def _resume_agent(pane, session):
    """Start HQ's Claude in `pane` on its saved conversation. herdr giving up on the wait while the Claude came up
    counts as started."""
    try:
        _start_agent(pane, "--resume", session)
    except herdr.HerdrError as exc:
        if not retire.agent_owned(paths.hq_name()):
            raise ResumeError(f"resuming HQ's conversation {session} failed ({exc}); it is still there "
                              f"(claude --resume {session}). Start a fresh HQ on purpose with `athena hq rotate`.")


def hq(cwd=None, focus=True, start_board=True, start_dash=None, fresh=True) -> dict:
    """Create or focus HQ. An HQ whose Claude is gone is brought back on its saved conversation (`--resume`), in its
    surviving space or in a new one when the space went too; a fresh conversation starts only for an HQ that never
    had one, and never when `fresh` is False (doctor), which returns {"missing": True} instead."""
    start_dash = start_board if start_dash is None else start_dash
    current = load()
    session = own_session(current)
    if current.get("pane") and _alive(current["pane"]):
        sync_session(_session())
        keep_label(current, owned=True)
        if focus and current.get("workspace"):
            herdr.call("workspace", "focus", current["workspace"])
        _helpers(start_board, start_dash)
        return {**current, "created": False}
    if not session and not fresh:
        return {**current, "created": False, "missing": True}
    if current.get("pane") and _workspace_is_hq(current):
        # The hq space survived but its Claude did not (exited, or a herdr restart): bring it back there.
        if session:
            _resume_agent(current["pane"], session)
        else:
            _start_agent(current["pane"])
        _remember_session(current)
        keep_label(current, owned=True)
        _routine(RESTART_ROUTINE if session else None)
        if focus:
            herdr.call("workspace", "focus", current["workspace"])
        _helpers(start_board, start_dash)
        return {**load(), "created": False, "restarted": True, "resumed": bool(session)}
    cwd = os.path.realpath(os.path.expanduser(cwd or current.get("cwd") or paths.projects_dir()))
    claim = paths.claim_file(cwd)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("hq\n")
    try:
        created = herdr.call("workspace", "create", "--cwd", cwd, "--label", LABEL, "--focus" if focus else "--no-focus")
        pane = created["root_pane"]["pane_id"]
        try:
            if session:
                _resume_agent(pane, session)
            else:
                _start_agent(pane)
        except (ResumeError, herdr.HerdrError):
            # Never leave the space behind: hq.json does not know it, so every retry would make another.
            try:
                herdr.call("workspace", "close", created["workspace"]["workspace_id"], timeout=30)
            except herdr.HerdrError:
                pass
            raise
    finally:
        claim.unlink(missing_ok=True)
    record = {**current, "pane": pane, "workspace": created["workspace"]["workspace_id"],
              "terminal": created["root_pane"].get("terminal_id"), "cwd": cwd}
    _file().write_text(json.dumps(record, indent=2))
    _remember_session(record)
    keep_label(record, owned=True)
    _routine(RESTART_ROUTINE if session else None)
    _helpers(start_board, start_dash)
    return {**load(), "created": True, "resumed": bool(session)}


RESTART_ROUTINE = ("HQ restarted: run your session-start routine (athena status --pr; the athena-watch mod restarts "
                   "athena watch and its stacked-PR watch on its own, so arm no Monitor unless the skill's fallback applies; "
                   "check `athena stack` lists the chains you follow, act on `athena ask queue`), then tell me what changed.")


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


# Not in `claude plugin marketplace list --json`, but refreshed by a plain `claude plugin marketplace update`.
BUILT_IN_MARKETPLACE = "anthropic-plugin-directory"


def _json(proc, default):
    try:
        return json.loads(proc.stdout) if proc else default
    except ValueError:
        return default


def update_claude():
    """`claude update`, the marketplaces and the user-scope plugins. A failure is a warning, never a stop.

    `claude update` runs alongside the rest. The marketplaces update side by side, one process per marketplace (in the
    lab, Claude Code 2.1.296, each landed in ~/.claude/plugins/known_marketplaces.json). The user-scope plugins then
    update from the refreshed marketplaces one at a time: an update that installs a new version rewrites
    installed_plugins.json, and nothing read-only says in advance which plugins have one.
    Project-scope installs belong to other checkouts (often live workers'), so they are left alone. No `--yes`:
    a plugin whose marketplace changed its install command waits for a person to accept it by hand.
    """
    claude = _claude_bin()
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    warnings = []

    def run(*args, timeout=300, quiet=False):
        started = time.monotonic()
        try:
            proc = subprocess.run([claude, *args], capture_output=True, text=True, timeout=timeout, env=env,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            proc = None
            detail = str(exc)
        else:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            took = f"({time.monotonic() - started:.1f}s)"
            if proc.returncode == 0:
                _say(f"claude {' '.join(args)} {took}: {'ok' if quiet else (proc.stdout or '').strip()[-200:] or 'ok'}")
                return proc
        warnings.append(f"claude {' '.join(args)} failed: {detail}")
        _say("warning: " + warnings[-1])
        return None

    def marketplaces():
        names = [m["name"] for m in _json(run("plugin", "marketplace", "list", "--json", timeout=60, quiet=True), [])
                 if isinstance(m, dict) and m.get("name")]
        if not names:
            run("plugin", "marketplace", "update")
            return
        names += [BUILT_IN_MARKETPLACE] if BUILT_IN_MARKETPLACE not in names else []
        list(pool.map(lambda name: run("plugin", "marketplace", "update", name), names))

    with ThreadPoolExecutor(max_workers=32) as pool:
        cli = pool.submit(run, "update")
        listed = pool.submit(run, "plugin", "list", "--json", timeout=60, quiet=True)
        marketplaces()
        ids = []
        for p in _json(listed.result(), []):
            if isinstance(p, dict) and p.get("scope") == "user" and p.get("id") and p["id"] not in ids:
                ids.append(p["id"])
        for pid in ids:
            run("plugin", "update", pid, "--scope", "user", timeout=120)
        cli.result()
    return warnings


def update_stamp_file():
    return paths.ensure() / "hq-update.json"


def _last_update():
    """When the updates last ran without a warning (epoch seconds), or None."""
    try:
        return float(json.loads(update_stamp_file().read_text())["last_success"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _stamp_update():
    """Record a run with no warnings. A stamp that cannot be written is a warning: the next restart updates again."""
    target = update_stamp_file()
    tmp = target.with_name(target.name + ".tmp%d" % os.getpid())
    try:
        tmp.write_text(json.dumps({"last_success": time.time()}) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        _say(f"warning: could not write {target} ({exc}); the next restart updates again")
        with contextlib.suppress(OSError):
            tmp.unlink()


def _update_interval_hours():
    default = config.DEFAULTS["hq_update_interval_hours"]
    try:
        hours = float(config.get("hq_update_interval_hours"))
    except (config.ConfigError, TypeError, ValueError):
        return default
    return hours if hours >= 0 else default


def _why_skip_updates():
    """Why the updates can wait this time, or None when they are due: they ran without a warning within the
    configured interval (0: always update)."""
    hours, last = _update_interval_hours(), _last_update()
    if not hours or last is None:
        return None
    ago = (time.time() - last) / 3600
    if 0 <= ago < hours:
        return (f"the last successful run was {ago:.1f}h ago, under hq_update_interval_hours ({hours:g}); "
                "`athena hq restart --update` forces them")
    return None


def _wait_idle():
    """When HQ runs the restart about itself it is still finishing that turn: /exit typed now would be queued."""
    wait = int(float(os.environ.get("ATHENA_HQ_IDLE_WAIT") or 300) * 1000)
    try:
        herdr.call("agent", "wait", paths.hq_name(), "--until", "idle", "--until", "done", "--timeout", wait,
                   timeout=wait / 1000 + 15)
        return True
    except herdr.HerdrError:
        return False


def restart_now(update=True, force_update=False, unattended=False) -> dict:
    """The restart itself, in the detached child: update, exit HQ's Claude, resume its conversation in the same pane.

    The updates are skipped when they ran without a warning within config hq_update_interval_hours, unless
    `force_update`; `update=False` skips them whatever. `unattended` (athena doctor) never starts a fresh HQ and
    never stops HQ's background work: with no known conversation, or with subagents, monitors or shells running, it
    leaves HQ as it is and fails."""
    _lock()
    try:
        return _restart(update, force_update, unattended)
    finally:
        _unlock()


def _restart(update, force_update=False, unattended=False):
    record = load()
    name, pane = paths.hq_name(), record.get("pane")
    if not pane:
        raise RestartError("there is no HQ to restart: start one with `athena hq`")
    began = time.monotonic()

    def took(since):
        return f"{time.monotonic() - since:.1f}s"
    skip = None if force_update else _why_skip_updates() if update else "--no-update"
    if skip:
        _say(f"updates skipped: {skip}")
    else:
        if force_update:
            _say("updates forced by --update")
        # Before the exit: the resume that follows is the restart that applies what they installed.
        if not update_claude():
            _stamp_update()
        _say(f"updates took {took(began)}")
    session = trusted_session(record)
    if unattended and not session:
        msg = (f"HQ's conversation id is not known ({name} in pane {pane}); an unattended restart never starts a fresh "
               "HQ, so nothing was restarted. Run `athena hq restart` by hand")
        _say("FAILED: " + msg)
        _notify("athena: HQ restart skipped", msg)
        raise RestartError(msg)
    if retire.agent_owned(name):
        if not _wait_idle():
            # Typing /exit and Enter into a permission dialog would answer it.
            msg = f"HQ ({name} in pane {pane}) is busy or waiting on you; not restarting. Retry when it is idle"
            _say("FAILED: " + msg)
            _notify("athena: HQ restart skipped", msg)
            raise RestartError(msg)
        if not composer.wait_clear(name):
            msg = f"someone is typing in HQ's input box ({name} in pane {pane}); not restarting. Retry once it is sent"
            _say("FAILED: " + msg)
            _notify("athena: HQ restart skipped", msg)
            raise RestartError(msg)
        grace = float(os.environ.get("ATHENA_EXIT_GRACE") or 20)
        exiting = time.monotonic()
        try:
            exited = retire.exit_claude(name, grace, stop_tasks=not unattended)
        except retire.BackgroundWork as exc:
            msg = f"{exc}; HQ was not restarted. Doctor tries again later"
            _say("FAILED: " + msg)
            _notify("athena: HQ restart skipped", msg)
            raise RestartError(msg)
        if not exited:
            msg = f"HQ's Claude ({name} in pane {pane}) did not exit; it is still running and nothing was restarted"
            _say("FAILED: " + msg)
            _notify("athena: HQ restart failed", msg)
            raise RestartError(msg)
        _say(f"HQ's Claude exited in {took(exiting)} (conversation {session or 'unknown'})")
    else:
        _say(f"HQ's Claude was not running; resuming conversation {session or 'unknown'}")
    time.sleep(0.2)  # herdr reports the agent gone a moment after the shell is back; this is only a margin
    resuming = time.monotonic()
    if session:
        try:
            _start_agent(pane, "--resume", session)
        except herdr.HerdrError as exc:
            if not retire.agent_owned(name):
                msg = (f"resuming conversation {session} failed ({exc}); nothing was started in its place. It is still "
                       f"there (claude --resume {session}); `athena doctor` tries again, or start a fresh HQ on purpose "
                       "with `athena hq rotate`")
                _say("FAILED: " + msg)
                _notify("athena: HQ not resumed", msg)
                raise RestartError(msg)
            _say(f"herdr gave up waiting ({exc}), but the resumed Claude is up")
    else:
        return _fallback("no conversation id was known for HQ")
    _remember_session(load())
    keep_label(owned=True)
    _routine(RESTART_ROUTINE)
    _say(f"HQ resumed conversation {session} in pane {pane} in {took(resuming)}; the restart took {took(began)} in all")
    return {"resumed": True, "session": session, "pane": pane}


def _fallback(why):
    msg = f"{why}: starting a fresh HQ conversation instead. The old one is still there: claude --resume <id>"
    _say("FALLBACK: " + msg)
    _notify("athena: HQ restarted without its conversation", msg)
    result = hq(focus=False, start_board=False, start_dash=False)
    _say(f"fresh HQ started in pane {result.get('pane')}")
    return {**result, "fallback": True, "reason": why}


def restart(update=True, force_update=False, unattended=False) -> dict:
    """Start the restart detached and return at once: when HQ runs this about itself, exiting HQ must not kill it.
    `unattended`: see restart_now."""
    pid = _holder()
    if pid:
        raise RestartError(f"an HQ restart is already running (pid {pid}); see {_log_file()}")
    log = _log_file()
    argv = [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "athena"), "hq", "restart", "--child"]
    if not update:
        argv.append("--no-update")
    elif force_update:
        argv.append("--update")
    if unattended:
        argv.append("--unattended")
    with open(log, "a") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                cwd=str(paths.PLUGIN_ROOT))
    return {"pid": proc.pid, "log": str(log), "message": f"restarting HQ; log at {log}"}


ROTATE_ROUTINE = ("Fresh HQ after `athena hq rotate`: load the athena skill, then read {handoff} in full (your fleet, "
                  "PRs, every open item by name, in-flight threads, notes tail and standing orders). Run the skill's session start "
                  "(§1) from it, including §1a: check the athena-watch mod runs athena watch, re-create the fallback "
                  "sweep cron with the sweep subagent brief, act on `athena ask queue`. Then tell me in at most five "
                  "lines what needs me and what is live.")


def _rotate_prompt():
    return ROTATE_ROUTINE.format(handoff=handoff.handoff_file())


def rotate(threads=None, dry_run=False, if_context_over=None) -> dict:
    """Write the handoff, then replace HQ's Claude with a fresh conversation that starts from it (detached, as restart).

    The handoff is written here, before anything exits, so a failure shows in HQ and the file exists for the new HQ.
    `dry_run` makes no herdr call and writes nothing: it returns the handoff, the launch argv and the first prompt.
    `if_context_over`: rotate only when HQ's context is readable and at least that full.
    """
    record = load()
    pane, session = record.get("pane"), record.get("session")
    if if_context_over is not None:
        ctx = handoff.context(session if dry_run else (_session() or session))
        if not ctx:
            return {"rotated": False, "message": "not rotating: HQ's context usage is not readable (no transcript "
                                                 f"for conversation {session or 'unknown'})"}
        if ctx["percent"] < if_context_over:
            return {"rotated": False, "context": ctx,
                    "message": f"not rotating: HQ's context is {handoff.describe(ctx)}, under {if_context_over:g}%"}
    if dry_run:
        argv = _start_argv(pane or "<hq pane>", write=False)
        text = handoff.build(threads=threads, previous_session=session, dry_run=True)
        return {"dry_run": True, "handoff": text, "launch": argv, "prompt": _rotate_prompt(),
                "command": " ".join(["herdr", *(shlex.quote(a) for a in argv)])}
    if not pane:
        raise RestartError("there is no HQ to rotate: start one with `athena hq`")
    pid = _holder()
    if pid:
        raise RestartError(f"an HQ restart or rotation is already running (pid {pid}); see {_log_file()}")
    if threads is not None:
        # Kept before the child's idle wait: if HQ is busy the rotation is skipped, and the next one reads them here.
        handoff.threads_file().write_text(threads)
    target = handoff.write(handoff.build(threads=threads, previous_session=_session() or session))
    log = _log_file()
    argv = [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "athena"), "hq", "rotate", "--child"]
    with open(log, "a") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                cwd=str(paths.PLUGIN_ROOT))
    return {"rotated": True, "pid": proc.pid, "log": str(log), "handoff": str(target),
            "message": f"handoff written to {target}; rotating HQ once it is idle; log at {log}"}


def _new_session(old):
    """The fresh HQ's conversation id: herdr may report none, or still the old one, for a moment after the start."""
    deadline = time.monotonic() + float(os.environ.get("ATHENA_HQ_SESSION_WAIT") or 10)
    while True:
        session = _session()
        if session and session != old:
            return session
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def rotate_now() -> dict:
    """The rotation itself, in the detached child: exit HQ's Claude, start a fresh one in the same pane."""
    _lock()
    try:
        return _rotate()
    finally:
        _unlock()


def _rotate():
    record = load()
    name, pane = paths.hq_name(), record.get("pane")
    if not pane:
        raise RestartError("there is no HQ to rotate: start one with `athena hq`")
    old = _session() or record.get("session")
    if retire.agent_owned(name):
        # The same clean exit as a restart: never /exit into a running turn or a permission dialog, never kill.
        if not _wait_idle():
            msg = f"HQ ({name} in pane {pane}) is busy or waiting on you; not rotating. Retry when it is idle"
            _say("FAILED: " + msg)
            _notify("athena: HQ rotation skipped", msg)
            raise RestartError(msg)
        if not composer.wait_clear(name):
            # /exit typed now would join the human's half-typed message and send it.
            msg = f"someone is typing in HQ's input box ({name} in pane {pane}); not rotating. Retry once it is sent"
            _say("FAILED: " + msg)
            _notify("athena: HQ rotation skipped", msg)
            raise RestartError(msg)
        if not retire.exit_claude(name, float(os.environ.get("ATHENA_EXIT_GRACE") or 20)):
            msg = f"HQ's Claude ({name} in pane {pane}) did not exit; it is still running and nothing was rotated"
            _say("FAILED: " + msg)
            _notify("athena: HQ rotation failed", msg)
            raise RestartError(msg)
        _say(f"HQ's Claude exited (conversation {old or 'unknown'}, still resumable with claude --resume)")
    time.sleep(0.2)  # herdr reports the agent gone a moment after the shell is back; this is only a margin
    # Forget the old conversation as HQ's before the fresh one starts: athena's hook then owns the fresh one's first
    # SessionStart (hq_owns: none known yet) and vouches for it in hq-start.json, and nothing vouches for the old
    # one any more, so a `claude -p --resume <old>` later run from HQ's Bash can never become HQ again.
    _forget_session(old)
    try:
        _start_agent(pane)
    except herdr.HerdrError as exc:
        if not retire.agent_owned(name):
            kept = _restore_session(old)
            msg = (f"starting the fresh HQ failed ({exc}); `athena hq` brings HQ back on conversation "
                   f"{kept or 'unknown'}. The handoff for a fresh HQ is at {handoff.handoff_file()}")
            _say("FAILED: " + msg)
            _notify("athena: HQ rotation failed", msg)
            raise RestartError(msg)
        _say(f"herdr gave up waiting ({exc}), but the fresh Claude is up")
    new = _new_session(old)
    record = load()
    record["previous_session"], record["session"] = old, new
    record["claude_args"] = _claude_args()
    _file().write_text(json.dumps(record, indent=2))
    _register_resume(pane, new, record["claude_args"])
    if new:
        # herdr's id, read right after this rotation's own start: HQ's own, as the hook would have said.
        _vouch(new, "rotate")
    if not new:
        # Never leave the old id as the session: `athena status` would read its full transcript and keep asking for
        # a rotation, and `athena hq restart` would resume the conversation this one replaced.
        _say("herdr does not report the fresh HQ's conversation id yet; hq.json records none until a restart or "
             "`athena hq` sees it")
    keep_label(owned=True)
    _routine(_rotate_prompt())
    threads = handoff.threads_file()
    if threads.exists():
        os.replace(threads, threads.with_name("threads.prev.md"))  # its threads are in the handoff now
    _say(f"HQ rotated in pane {pane}: conversation {old or 'unknown'} -> {record.get('session') or 'unknown'}")
    return {"rotated": True, "pane": pane, "previous_session": old, "session": record.get("session")}
