"""CLI parsing for --loop (Docker Compose entrypoint)."""

import os
import sys
import unittest
from unittest import mock

_AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import agent as agent_mod  # noqa: E402


class TestLoopCli(unittest.TestCase):
    def test_loop_flag_parsed(self):
        with mock.patch.object(sys, "argv", ["agent.py", "--loop"]):
            parsed = agent_mod.parse_args()
        self.assertTrue(parsed[-1])

    def test_loop_absent_by_default(self):
        with mock.patch.object(sys, "argv", ["agent.py"]):
            parsed = agent_mod.parse_args()
        self.assertFalse(parsed[-1])

    def test_report_interval_from_env(self):
        with mock.patch.dict(os.environ, {"SERVERMETRY_INTERVAL": "15"}):
            self.assertEqual(agent_mod._report_interval({"reportIntervalSeconds": 60}), 15)

    def test_report_interval_from_config(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SERVERMETRY_INTERVAL", None)
            self.assertEqual(agent_mod._report_interval({"reportIntervalSeconds": 120}), 120)

    def test_check_does_not_require_api_key(self):
        with mock.patch.object(sys, "argv", ["agent.py", "--check"]), \
             mock.patch.object(agent_mod, "_collect_metrics", return_value={
                 "os": "test", "kernelVersion": "1", "cpuUsagePercent": 0,
                 "cpuCores": 1, "cpuThreads": 1, "memUsagePercent": 0,
                 "memUsedMb": 0, "memTotalMb": 0, "diskUsages": [],
                 "networkInterfaces": [], "processCount": 0,
             }), \
             mock.patch.object(agent_mod, "ensure_config") as ensure, \
             mock.patch.object(agent_mod, "load_config", return_value=({}, "")), \
             mock.patch.object(agent_mod, "_print_check"):
            with self.assertRaises(SystemExit) as ctx:
                agent_mod.main()
        self.assertEqual(ctx.exception.code, 0)
        ensure.assert_not_called()

    def test_report_interval_clamped(self):
        with mock.patch.dict(os.environ, {"SERVERMETRY_INTERVAL": "1"}):
            self.assertEqual(agent_mod._report_interval({}), 10)


if __name__ == "__main__":
    unittest.main()
