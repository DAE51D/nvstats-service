import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import nvstats_poll as poll  # noqa: E402


class ProcScanTests(unittest.TestCase):
    def setUp(self):
        poll._nonclient_pids.clear()
        poll._scan_tick = 0

    def run_ticks(self, n, client_pids):
        scans = []

        def fake_fdinfo(pid_s):
            scans.append(int(pid_s))
            if int(pid_s) in client_pids:
                return {"types": ["compute"], "gpu_pct": None, "vram_kb": 1}
            return None

        with mock.patch.object(poll.os, "listdir", return_value=["10", "11", "12", "self"]), \
                mock.patch.object(poll, "_fdinfo_gpu_stats", fake_fdinfo), \
                mock.patch.object(poll, "_proc_cmdline", return_value=["x"]), \
                mock.patch.object(poll, "_proc_status", return_value={"Name": "x"}), \
                mock.patch.object(poll, "_username", return_value="u"), \
                mock.patch.object(poll, "owning_unit", return_value=None), \
                mock.patch.object(poll, "_proc_cpu_pct", return_value=0.0):
            for _ in range(n):
                result = poll.gpu_proc_sample()
        return scans, result

    def test_non_clients_are_rescanned_rarely_clients_every_tick(self):
        n = poll.NONCLIENT_RECHECK * 3
        scans, result = self.run_ticks(n, client_pids={10})
        self.assertEqual(scans.count(10), n)
        self.assertLess(scans.count(11), n // 2)
        self.assertGreaterEqual(scans.count(11), 3)  # but never forgotten
        self.assertEqual([p["pid"] for p in result], [10])

    def test_dead_pids_are_forgotten(self):
        self.run_ticks(2, client_pids=set())
        self.assertEqual(poll._nonclient_pids, {10, 11, 12})
        with mock.patch.object(poll.os, "listdir", return_value=["11"]), \
                mock.patch.object(poll, "_fdinfo_gpu_stats", return_value=None):
            poll.gpu_proc_sample()
        self.assertEqual(poll._nonclient_pids, {11})


if __name__ == "__main__":
    unittest.main()
