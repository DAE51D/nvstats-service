import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import nvstats_core as core  # noqa: E402


def proc(pid, vram, argv):
    return {
        "pid": pid,
        "name": argv[0].split("/")[-1],
        "user": "u",
        "unit": None,
        "argv": argv,
        "cmdline": " ".join(argv),
        "type": ["compute"],
        "gpu_pct": 1.0,
        "vram_kb": vram,
        "cpu_pct": 2.0,
        "rss_kb": 3,
    }


SAMPLE = {
    "schema": 1,
    "host": "h",
    "seq": 7,
    "ts": 1.0,
    "version": "x",
    "git_commit": "abc",
    "gpu": {"index": 0, "backend": "nvidia", "util": 5},
    "sys": {"cpu": 1.0},
    "llm": {
        "llama": [proc(1, 10, ["llama-server", "--api-key", "s3cret"])],
        "ollama_models": [{"name": "m"}],
    },
    "gpu_procs": [proc(1, 10, ["llama-server", "--api-key", "s3cret", "--port", "8080"])]
    + [proc(100 + i, 100 + i, ["p"]) for i in range(8)],
}


class RedactTests(unittest.TestCase):
    def test_masks_value_after_secret_flag(self):
        self.assertEqual(
            core.redact_argv(["srv", "--api-key", "abc", "--port", "1"]),
            ["srv", "--api-key", "***", "--port", "1"],
        )

    def test_masks_equals_form(self):
        self.assertEqual(core.redact_argv(["srv", "--token=abc"]), ["srv", "--token=***"])

    def test_flag_without_value_does_not_eat_next_option(self):
        self.assertEqual(
            core.redact_argv(["srv", "--no-auth", "--port", "1"]),
            ["srv", "--no-auth", "--port", "1"],
        )


class ViewTests(unittest.TestCase):
    def test_minimal_is_small_and_sorted(self):
        v = core.view_minimal(SAMPLE)
        self.assertEqual(set(v), {*core.META_KEYS, "gpu", "gpu_procs"})
        self.assertEqual(len(v["gpu_procs"]), core.MINIMAL_PROCS)
        vram = [p["vram_kb"] for p in v["gpu_procs"]]
        self.assertEqual(vram, sorted(vram, reverse=True))
        self.assertNotIn("argv", v["gpu_procs"][0])

    def test_minimal_redacts_by_default(self):
        v = core.view_minimal(SAMPLE, max_procs=20)
        llama = next(p for p in v["gpu_procs"] if p["pid"] == 1)
        self.assertNotIn("s3cret", llama["cmdline"])
        raw = core.view_minimal(SAMPLE, redact=False, max_procs=20)
        self.assertIn("s3cret", next(p for p in raw["gpu_procs"] if p["pid"] == 1)["cmdline"])

    def test_full_redaction_covers_llm_and_leaves_input_untouched(self):
        v = core.view_full(SAMPLE, redact=True)
        self.assertNotIn("s3cret", str(v["llm"]["llama"][0]["argv"]))
        self.assertEqual(v["llm"]["ollama_models"], [{"name": "m"}])
        self.assertIn("s3cret", SAMPLE["gpu_procs"][0]["argv"])

    def test_full_without_redaction_is_identity(self):
        self.assertIs(core.view_full(SAMPLE, redact=False), SAMPLE)


class AttachTests(unittest.TestCase):
    def test_llm_card_inherits_gpu_numbers(self):
        llm = {
            "llama": [{"pid": 5, "gpu_pct": None, "vram_kb": None, "type": []}],
            "ollama_models": [],
        }
        procs = [{"pid": 5, "gpu_pct": 3.0, "vram_kb": 99, "type": ["compute"]}]
        core._attach_gpu_to_llm(llm, procs)
        self.assertEqual(llm["llama"][0]["vram_kb"], 99)
        self.assertEqual(llm["llama"][0]["type"], ["compute"])


if __name__ == "__main__":
    unittest.main()
