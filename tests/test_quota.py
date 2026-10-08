import json
import os
import tempfile
import time
import unittest

from tests import helpers
from cos_lib import quota


def write_quota(path, used, age=0):
    data = {"fetched_at_unix": int(time.time() - age), "windows": [
        {"kind": "five_hour", "used_percent": used, "remaining_percent": 100 - used},
        {"kind": "weekly", "used_percent": 10.0},
    ]}
    with open(path, "w") as f:
        json.dump(data, f)


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = helpers.isolated_env(self.tmp.name)
        self.file = os.environ["COS_QUOTA_FILE"]

    def tearDown(self):
        self.env.restore()
        self.tmp.cleanup()

    def test_gate_blocks_above_threshold(self):
        write_quota(self.file, 90.0)
        ok, why = quota.gate(85)
        self.assertFalse(ok)
        self.assertIn("90", why)

    def test_gate_open_below_threshold(self):
        write_quota(self.file, 40.0)
        self.assertEqual(quota.gate(85)[0], True)

    def test_gate_open_when_stale_or_missing(self):
        self.assertTrue(quota.gate(85)[0])
        write_quota(self.file, 99.0, age=3600)
        self.assertIsNone(quota.five_hour_used())
        self.assertTrue(quota.gate(85)[0])


if __name__ == "__main__":
    unittest.main()
