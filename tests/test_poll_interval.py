import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import nvstats_core as core  # noqa: E402


class PollSecondsFromEnv(unittest.TestCase):
    def test_default_when_unset_or_empty(self):
        self.assertEqual(core.poll_seconds_from_env({}), 1.0)
        self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": "  "}), 1.0)

    def test_valid_values(self):
        self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": "0.5"}), 0.5)
        self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": "3"}), 3.0)

    def test_clamped(self):
        self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": "0.01"}), 0.25)
        self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": "-4"}), 0.25)
        self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": "999"}), 10.0)

    def test_garbage_falls_back_to_default(self):
        for bad in ("fast", "1,5", "nan"):
            self.assertEqual(core.poll_seconds_from_env({"NVSTATS_POLL_SECONDS": bad}), 1.0, bad)


class Cadence(unittest.TestCase):
    """gpu/sys follow the sample interval; the heavy scans stay at once per second."""

    def run_sampler(self, poll, seconds):
        calls = {"gpu": 0, "sys": 0, "llm": 0, "procs": 0}

        def counted(name, value):
            def fn(*a, **k):
                calls[name] += 1
                return value() if callable(value) else value
            return fn

        patches = [
            mock.patch.object(core, "gpu_sample", counted("gpu", {"uuid": "u", "backend": "amd"})),
            mock.patch.object(core, "sys_sample", counted("sys", {"cpu": 1.0})),
            mock.patch.object(core, "llm_sample", counted("llm", lambda: {"llama": []})),
            mock.patch.object(core, "ollama_ps", counted("llm", [])),
            mock.patch.object(core, "gpu_proc_sample", counted("procs", [])),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        s = core.Sampler(poll_seconds=poll)
        s.start()
        time.sleep(seconds)
        s.stop()
        return calls, s

    def test_default_polls_everything_each_second(self):
        calls, _ = self.run_sampler(1.0, 2.4)
        self.assertGreaterEqual(calls["gpu"], 3)
        self.assertEqual(calls["procs"], calls["gpu"])  # unchanged behaviour at the default rate

    def test_fast_polling_keeps_heavy_scans_at_one_per_second(self):
        calls, _ = self.run_sampler(0.25, 2.4)
        self.assertGreaterEqual(calls["gpu"], 8)        # ~4/s
        self.assertEqual(calls["sys"], calls["gpu"])
        self.assertLessEqual(calls["procs"], 4)         # ~1/s, not ~4/s
        self.assertGreaterEqual(calls["procs"], 2)

    def test_slower_than_a_second_polls_everything_each_time(self):
        calls, _ = self.run_sampler(1.5, 3.4)
        self.assertEqual(calls["procs"], calls["gpu"])

    def test_samples_stay_complete_between_heavy_refreshes(self):
        _, s = self.run_sampler(0.25, 1.0)
        latest = s.latest()
        for key in ("gpu", "sys", "llm", "gpu_procs", "seq", "ts"):
            self.assertIn(key, latest)


if __name__ == "__main__":
    unittest.main()
