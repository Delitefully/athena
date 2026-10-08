import tempfile
import unittest

from tests import helpers
from athena_lib import ledger, paths


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_fold_spawn_update_retire(self):
        ledger.append("spawn", "plt-1", branch="gabriel/plt-1-x", pane="w1:p1")
        ledger.append("update", "plt-1", pr="https://example/1")
        workers = ledger.workers()
        self.assertEqual(workers["plt-1"]["state"], "live")
        self.assertEqual(workers["plt-1"]["pane"], "w1:p1")
        self.assertEqual(workers["plt-1"]["pr"], "https://example/1")
        ledger.append("retire", "plt-1")
        self.assertEqual(ledger.workers()["plt-1"]["state"], "retired")

    def test_live_excludes_failed_and_retired(self):
        ledger.append("spawn", "a", pane="w1:p1")
        ledger.append("spawn", "b", pane="w2:p1")
        ledger.append("failed", "b", reason="timeout")
        ledger.append("spawn", "c", pane="w3:p1")
        ledger.append("retire", "c")
        self.assertEqual([w["name"] for w in ledger.live()], ["a"])
        self.assertEqual(ledger.workers()["b"]["reason"], "timeout")

    def test_respawn_after_retire_is_live_again(self):
        ledger.append("spawn", "a", pane="w1:p1")
        ledger.append("retire", "a")
        ledger.append("spawn", "a", pane="w9:p1")
        self.assertEqual(ledger.workers()["a"]["state"], "live")
        self.assertEqual(ledger.workers()["a"]["pane"], "w9:p1")

    def test_failed_respawn_does_not_inherit_old_ids(self):
        ledger.append("spawn", "a", pane="w2:p1", workspace="w2")
        ledger.append("retire", "a")
        ledger.append("failed", "a", reason="create failed", path="/new")
        w = ledger.workers()["a"]
        self.assertEqual(w["state"], "failed")
        self.assertNotIn("pane", w)
        self.assertNotIn("workspace", w)

    def test_ledger_tolerates_corrupt_line(self):
        ledger.append("spawn", "a", pane="w1:p1")
        with open(paths.state_dir() / "ledger.jsonl", "a") as f:
            f.write("{not json\n")
        ledger.append("update", "a", pr="x")
        self.assertEqual(ledger.workers()["a"]["pr"], "x")

    def test_find_by_path(self):
        ledger.append("spawn", "a", path="/x/y/a")
        self.assertEqual(ledger.find_by_path("/x/y/a/sub/dir")["name"], "a")
        self.assertIsNone(ledger.find_by_path("/x/y/ab"))


if __name__ == "__main__":
    unittest.main()
