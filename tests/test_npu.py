import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import nvstats_core as core  # noqa: E402
import nvstats_npu as npu  # noqa: E402
import nvstats_poll as poll  # noqa: E402


def fake_sysfs(driver="amdxdna"):
    """A /sys/class/accel look-alike with one accel0 bound to `driver`."""
    root = tempfile.mkdtemp()
    dev = os.path.join(root, "accel0", "device")
    os.makedirs(dev)
    drv = os.path.join(root, "drivers", driver)
    os.makedirs(drv)
    os.symlink(drv, os.path.join(dev, "driver"))
    with open(os.path.join(dev, "vbnv"), "w") as f:
        f.write("RyzenAI-npu5\n")
    with open(os.path.join(dev, "fw_version"), "w") as f:
        f.write("1.1.2.65\n")
    return root


def ctx(cid, pid, sub, comp, heap=0):
    return {
        "context_id": cid,
        "start_col": 0,
        "num_col": 8,
        "hwctx_id": cid,
        "pid": pid,
        "submissions": sub,
        "completions": comp,
        "migrations": 0,
        "preemptions": 0,
        "errors": 0,
        "priority": 0,
        "heap_usage": heap,
        "suspensions": 0,
    }


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class DiscoveryTests(unittest.TestCase):
    def test_no_accel_dir_means_no_npu(self):
        s = npu.NpuSampler(sysfs="/nonexistent/accel")
        self.assertEqual(s.sample(), (None, []))

    def test_unsupported_driver_is_present_but_not_readable(self):
        s = npu.NpuSampler(sysfs=fake_sysfs("intel_vpu"))
        info, procs = s.sample()
        self.assertTrue(info["present"])
        self.assertFalse(info["readable"])
        self.assertIn("intel_vpu", info["reason"])
        self.assertIsNone(info["util"])
        self.assertEqual(procs, [])

    def test_permission_denied_is_reported_not_raised(self):
        s = npu.NpuSampler(sysfs=fake_sysfs())
        with mock.patch("os.open", side_effect=PermissionError(13, "denied")):
            info, procs = s.sample()
        self.assertTrue(info["present"])
        self.assertFalse(info["readable"])
        self.assertTrue(info["reason"].startswith("permission"))
        self.assertIsNone(info["util"])


class ActivityTests(unittest.TestCase):
    def sampler(self):
        clock = Clock()
        s = npu.NpuSampler(sysfs=fake_sysfs(), now=clock)
        s._fd = 3  # skip opening a real device node
        return s, clock

    def run_ticks(self, s, clock, frames):
        out = []
        for frame in frames:
            with (
                mock.patch.object(npu, "_get_hwctx", return_value=frame),
                mock.patch.object(
                    npu, "_get_info", side_effect=npu.NpuError("unsupported")
                ),
            ):
                out.append(s.sample())
            clock.t += 0.5
        return out

    def test_idle_then_active_then_idle(self):
        s, clock = self.sampler()
        idle = [ctx(1, 42, 10, 10)]
        busy = lambda n: [ctx(1, 42, 10 + n, 10 + n - 1)]  # noqa: E731
        results = self.run_ticks(
            s, clock, [idle, idle, busy(1), busy(2), busy(3), busy(4)]
        )
        self.assertEqual(results[1][0]["util"], 0.0)
        self.assertGreater(results[-1][0]["util"], 50.0)
        self.assertTrue(results[-1][0]["readable"])
        self.assertEqual(results[-1][0]["util_source"], "activity")
        # Window is 2 s: four idle ticks later the figure is back to 0.
        tail = self.run_ticks(s, clock, [[ctx(1, 42, 14, 14)]] * 6)
        self.assertEqual(tail[-1][0]["util"], 0.0)

    def test_completions_alone_count_as_busy(self):
        s, clock = self.sampler()
        r = self.run_ticks(
            s, clock, [[ctx(1, 42, 5, 5)], [ctx(1, 42, 9, 9)], [ctx(1, 42, 14, 14)]]
        )
        self.assertGreater(r[-1][0]["util"], 0.0)
        self.assertGreater(r[-1][0]["cmds_per_s"], 0.0)

    def test_process_rows_are_marked_npu(self):
        s, clock = self.sampler()
        info, procs = self.run_ticks(s, clock, [[ctx(1, os.getpid(), 3, 2, 4096)]])[0]
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0]["pid"], os.getpid())
        self.assertEqual(procs[0]["type"], ["npu"])
        self.assertEqual(procs[0]["vram_kb"], 4)
        self.assertEqual(info["tasks"], 1)


class LlmAndViewTests(unittest.TestCase):
    def test_flm_serve_is_detected(self):
        argv = [
            "/var/cache/lemonade/bin/flm/npu/flm-real",
            "serve",
            "qwen3.5:9b",
            "--ctx-len",
            "131072",
            "--port",
            "8001",
        ]
        self.assertEqual(poll._classify_llm(argv), "flm")
        self.assertEqual(poll._llm_model("flm", argv), "qwen3.5:9b")

    def test_flm_housekeeping_commands_are_not_llm_processes(self):
        self.assertIsNone(poll._classify_llm(["flm", "list", "--filter", "installed"]))
        self.assertIsNone(poll._classify_llm(["flm", "validate"]))

    def test_minimal_view_omits_npu_without_one(self):
        sample = {k: 0 for k in core.META_KEYS}
        sample.update(gpu={}, sys={}, gpu_procs=[], npu=None)
        self.assertNotIn("npu", core.view_minimal(sample))

    def test_minimal_view_keeps_npu_and_reserves_slots_for_its_processes(self):
        def p(pid, vram, types):
            return {
                "pid": pid,
                "name": "n",
                "user": "u",
                "unit": None,
                "argv": ["n"],
                "cmdline": "n",
                "type": types,
                "gpu_pct": 1.0,
                "vram_kb": vram,
                "cpu_pct": 1.0,
                "rss_kb": 1,
            }

        sample = {k: 0 for k in core.META_KEYS}
        sample.update(
            gpu={},
            sys={},
            npu={"present": True, "readable": False, "reason": "permission"},
            gpu_procs=[p(i, 1000 - i, ["compute"]) for i in range(9)]
            + [p(99, None, ["npu"])],
        )
        out = core.view_minimal(sample)
        self.assertEqual(out["npu"]["reason"], "permission")
        self.assertEqual(len(out["gpu_procs"]), core.MINIMAL_PROCS)
        npu_rows = [r for r in out["gpu_procs"] if r.get("type") == ["npu"]]
        self.assertEqual([r["pid"] for r in npu_rows], [99])
        gpu_rows = [r for r in out["gpu_procs"] if "type" not in r]
        self.assertTrue(gpu_rows)  # GPU rows stay unmarked


if __name__ == "__main__":
    unittest.main()
