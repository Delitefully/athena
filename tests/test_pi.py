"""Pi as a second worker agent. Claude stays the default, and its launch is byte for byte what it was before Pi."""
import json
import os
from pathlib import Path

from harness import ROOT, Case

from athena.caps import spawn
from athena.kernel import caps, ledger, store

FIXTURE = ROOT / "tests" / "fixtures" / "claude-launch.json"


def _neutral(text, case) -> str:
    """The launch with this test's own places named by placeholders, so it compares across runs and checkouts."""
    for real, mark in ((str(case.tmp), "<tmp>"), (str(ROOT), "<root>")):
        text = text.replace(real, mark).replace(os.path.realpath(real), mark)
    return text


class ClaudeLaunchUnchanged(Case):
    def _spawn(self, **more):
        self.write_config(worktree_root=str(self.tmp / "wt"))
        spawn.spawn(caps.CTX, self.repo(), "t/a", "a", prompt="hi", timeout=0.1, fetch=False, **more)
        launcher = store.path("run", "a.sh").read_text()
        settings = json.loads(store.path("settings", "a.json").read_text())
        return {"launcher": _neutral(launcher, self),
                "settings": json.loads(_neutral(json.dumps(settings), self)),
                "claude_args": json.loads(_neutral(json.dumps(ledger.get("a")["claude_args"]), self))}

    def test_a_spawn_without_the_agent_flag_launches_claude_as_main_did(self):
        """tests/fixtures/claude-launch.json was written from main before Pi was added: the launcher script, the worker's
        settings and the ledger's claude_args are what main made."""
        seen = self._spawn()
        if os.environ.get("WRITE_CLAUDE_LAUNCH_FIXTURE"):
            FIXTURE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
        self.assertEqual(seen, json.loads(FIXTURE.read_text()))
