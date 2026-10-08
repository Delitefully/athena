import json
import tempfile
import unittest
from pathlib import Path

from tests import helpers
from athena_lib import herdr


def write_scenario(tmp, rules):
    Path(tmp, "scenario.json").write_text(json.dumps(rules))


def logged_calls(tmp):
    log = Path(tmp, "herdr.log")
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


class HerdrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_call_parses_result(self):
        write_scenario(self.tmp.name, [{"match": ["pane", "get"], "stdout": {"result": {"pane": {"pane_id": "w1:p1"}}}}])
        self.assertEqual(herdr.call("pane", "get", "w1:p1")["pane"]["pane_id"], "w1:p1")
        self.assertEqual(logged_calls(self.tmp.name), [["pane", "get", "w1:p1"]])

    def test_call_raises_on_error_json(self):
        err = {"error": {"code": "agent_pane_busy", "message": "busy"}, "id": "x"}
        write_scenario(self.tmp.name, [{"match": ["agent", "start"], "stdout": "", "stderr": json.dumps(err), "exit": 1}])
        with self.assertRaises(herdr.HerdrError) as ctx:
            herdr.call("agent", "start", "a")
        self.assertEqual(ctx.exception.code, "agent_pane_busy")

    def test_error_in_stdout_raises(self):
        err = {"error": {"code": "timeout", "message": "timed out"}, "id": "x"}
        write_scenario(self.tmp.name, [{"match": ["agent", "wait"], "stdout": err, "exit": 0}])
        with self.assertRaises(herdr.HerdrError) as ctx:
            herdr.call("agent", "wait", "a")
        self.assertEqual(ctx.exception.code, "timeout")

    def test_times_limits_rule(self):
        write_scenario(self.tmp.name, [
            {"match": ["agent", "get"], "stdout": {"result": {"n": 1}}, "times": 1},
            {"match": ["agent", "get"], "stdout": {"result": {"n": 2}}},
        ])
        self.assertEqual(herdr.call("agent", "get", "a")["n"], 1)
        self.assertEqual(herdr.call("agent", "get", "a")["n"], 2)


if __name__ == "__main__":
    unittest.main()
