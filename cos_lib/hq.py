"""The chief of staff's own space: create it once, then focus it."""
import json
import os
from pathlib import Path

from cos_lib import board, herdr, paths


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
        return agent.get("agent") == "claude"
    except herdr.HerdrError:
        return False


def hq(cwd=None, focus=True, start_board=True) -> dict:
    current = load()
    if current.get("pane") and _alive(current["pane"]):
        if focus and current.get("workspace"):
            herdr.call("workspace", "focus", current["workspace"])
        if start_board:
            board.start_watcher()
        return {**current, "created": False}
    cwd = os.path.realpath(os.path.expanduser(cwd or os.environ.get("COS_HQ_CWD") or "~/Developer"))
    claim = paths.claim_file(cwd)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("hq\n")
    try:
        created = herdr.call("workspace", "create", "--cwd", cwd, "--label", "hq", "--focus" if focus else "--no-focus")
        pane = created["root_pane"]["pane_id"]
        settings = paths.ensure("settings") / "hq.json"
        settings.write_text(json.dumps({"crossSessionInbound": "accept",
                                        "env": {"COS_ROLE": "hq", "COS_STATE_DIR": str(paths.state_dir())}}, indent=2))
        args = ["--name", paths.hq_name(), "--append-system-prompt-file", str(paths.PLUGIN_ROOT / "hq.md"),
                "--settings", str(settings)]
        if os.environ.get("COS_PLUGIN_DIR"):
            args += ["--plugin-dir", os.environ["COS_PLUGIN_DIR"]]
        herdr.call("agent", "start", paths.hq_name(), "--kind", "claude", "--pane", pane, "--timeout", "120000",
                   "--", *args, timeout=200)
    finally:
        claim.unlink(missing_ok=True)
    record = {"pane": pane, "workspace": created["workspace"]["workspace_id"],
              "terminal": created["root_pane"].get("terminal_id"), "cwd": cwd}
    _file().write_text(json.dumps(record, indent=2))
    herdr.call("agent", "prompt", paths.hq_name(),
               "Start your HQ routine: run `cos status`, start `cos watch` under Monitor, then tell me what is live and wait.")
    if start_board:
        board.start_watcher()
    return {**record, "created": True}
