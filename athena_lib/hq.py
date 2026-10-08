"""The chief of staff's own space: create it once, then focus it."""
import json
import os
from pathlib import Path

from athena_lib import board, herdr, paths


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


def _start_agent(pane):
    herdr.call("agent", "start", paths.hq_name(), "--kind", "claude", "--pane", pane, "--timeout", "120000",
               "--", *_claude_args(), timeout=200)


def _workspace_is_hq(record) -> bool:
    try:
        ws = herdr.call("workspace", "get", record["workspace"], timeout=10).get("workspace", {})
    except (herdr.HerdrError, KeyError):
        return False
    return ws.get("label") == "athena"


def _routine():
    herdr.call("agent", "prompt", paths.hq_name(),
               "Start your HQ routine: run `athena status`, start `athena watch` under Monitor, then tell me what is live and wait.")


def hq(cwd=None, focus=True, start_board=True) -> dict:
    current = load()
    if current.get("pane") and _alive(current["pane"]):
        if focus and current.get("workspace"):
            herdr.call("workspace", "focus", current["workspace"])
        if start_board:
            board.start_watcher()
        return {**current, "created": False}
    if current.get("pane") and _workspace_is_hq(current):
        # The hq space survived but its Claude did not (exited, or a herdr restart): restart it there.
        _start_agent(current["pane"])
        _routine()
        if focus:
            herdr.call("workspace", "focus", current["workspace"])
        if start_board:
            board.start_watcher()
        return {**current, "created": False, "restarted": True}
    cwd = os.path.realpath(os.path.expanduser(cwd or os.environ.get("ATHENA_HQ_CWD") or "~/Developer"))
    claim = paths.claim_file(cwd)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("hq\n")
    try:
        created = herdr.call("workspace", "create", "--cwd", cwd, "--label", "athena", "--focus" if focus else "--no-focus")
        pane = created["root_pane"]["pane_id"]
        _start_agent(pane)
    finally:
        claim.unlink(missing_ok=True)
    record = {"pane": pane, "workspace": created["workspace"]["workspace_id"],
              "terminal": created["root_pane"].get("terminal_id"), "cwd": cwd}
    _file().write_text(json.dumps(record, indent=2))
    _routine()
    if start_board:
        board.start_watcher()
    return {**record, "created": True}
