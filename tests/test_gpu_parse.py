import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from nvstats_gpu import GPU_KEYS, parse_nvidia_row  # noqa: E402

# Captured from `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`
# (RTX 5060 Ti, driver 595.99.02).
ROW = (
    "0, NVIDIA GeForce RTX 5060 Ti, GPU-2bb45349-6461-e802-99c1-098bae63ad20, "
    "595.99.02, 31, 0, 48, 10410, 16311, 3.55, 180.00, 0, 210, 405, 3120, 14001, "
    "1, 3, 8, 16, P8"
)
THROTTLE_IDLE = ", Not Active, Not Active, Not Active, Not Active"
THROTTLE_POWER = ", Not Active, Active, Not Active, Active"


class NvidiaParseTests(unittest.TestCase):
    def test_full_row(self):
        g = parse_nvidia_row(ROW + THROTTLE_IDLE, True)
        self.assertEqual(set(g), set(GPU_KEYS))
        self.assertEqual(g["name"], "NVIDIA GeForce RTX 5060 Ti")
        self.assertEqual(g["driver"], "595.99.02")
        self.assertEqual((g["mem_used"], g["mem_total"]), (10410, 16311))
        self.assertEqual(g["power_cap_w"], 180)
        self.assertEqual(g["clock_gfx_max"], 3120)
        self.assertEqual((g["pcie_gen"], g["pcie_gen_max"]), (1, 3))
        self.assertEqual((g["pcie_width"], g["pcie_width_max"]), (8, 16))
        self.assertEqual(g["pstate"], "P8")
        self.assertEqual(g["throttle"], "None")
        self.assertEqual(g["backend"], "nvidia")

    def test_throttle_reasons(self):
        g = parse_nvidia_row(ROW + THROTTLE_POWER, True)
        self.assertEqual(g["throttle"], "SW Power Cap, SW Thermal")

    def test_throttle_unsupported_is_null(self):
        self.assertIsNone(parse_nvidia_row(ROW, False)["throttle"])

    def test_na_cells_become_none(self):
        row = ROW.replace(", 0, 210,", ", [N/A], 210,")  # fan.speed
        self.assertIsNone(parse_nvidia_row(row, False)["fan_pct"])

    def test_short_row_rejected(self):
        self.assertIsNone(parse_nvidia_row("0, Foo", False))


class AmdMemoryTests(unittest.TestCase):
    GIB = 1024 ** 3

    def memory(self, vram_type, vram_total, gtt_total, used=1):
        import nvstats_gpu

        files = {
            "/d/mem_info_vram_used": used * self.GIB,
            "/d/mem_info_vram_total": vram_total,
            "/d/mem_info_gtt_used": 2 * used * self.GIB,
            "/d/mem_info_gtt_total": gtt_total,
        }
        orig = nvstats_gpu._read_int
        nvstats_gpu._read_int = files.get
        try:
            return nvstats_gpu._amd_memory("/d", vram_type)
        finally:
            nvstats_gpu._read_int = orig

    def test_apu_reports_gtt(self):
        # Radeon 8060S: 2 GiB LPDDR5 carve-out, ~78 GiB GTT pool.
        self.assertEqual(self.memory("LPDDR5", 2 * self.GIB, 78 * self.GIB), (2048, 78 * 1024))

    def test_discrete_reports_vram(self):
        self.assertEqual(self.memory("GDDR6", 16 * self.GIB, 32 * self.GIB), (1024, 16 * 1024))

    def test_unknown_type_falls_back_on_size(self):
        self.assertEqual(self.memory(None, 2 * self.GIB, 78 * self.GIB)[1], 78 * 1024)
        self.assertEqual(self.memory(None, 16 * self.GIB, 32 * self.GIB)[1], 16 * 1024)

    def test_missing_files_give_none(self):
        import nvstats_gpu

        orig = nvstats_gpu._read_int
        nvstats_gpu._read_int = lambda path: None
        try:
            self.assertEqual(nvstats_gpu._amd_memory("/d", "GDDR6"), (None, None))
        finally:
            nvstats_gpu._read_int = orig


class AmdParseTests(unittest.TestCase):
    def test_val_accepts_numeric_strings(self):
        from nvstats_gpu import _max_level, _val

        self.assertEqual(_val({"value": "2900", "unit": "MHz"}), 2900)
        self.assertEqual(_val({"value": 16.23}), 16.23)
        self.assertIsNone(_val("N/A"))
        self.assertIsNone(_val(None))
        # Shape from `amd-smi static --json` on a Radeon 8060S.
        levels = {
            "Level 0": {"value": "600", "unit": "MHz"},
            "Level 1": {"value": "692", "unit": "MHz"},
            "Level 2": {"value": "2900", "unit": "MHz"},
        }
        self.assertEqual(_max_level(levels), 2900)


if __name__ == "__main__":
    unittest.main()
