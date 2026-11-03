"""GPU sampling for nvstats-service.

One GPU is reported, chosen with NVSTATS_GPU_INDEX (default 0). Backends:

* nvidia -- one `nvidia-smi --query-gpu` call per sample.
* amd    -- everything dynamic from sysfs (gpu_busy_percent, mem_info_*,
            hwmon, pp_dpm_mclk, PCIe link files): no subprocess per sample.
            `amd-smi static --json` is run exactly once for identity and
            clock/PCIe ceilings.

NVSTATS_GPU_BACKEND=nvidia|amd forces a backend on hosts that have both.
Every field is present in the result; anything the hardware or driver does
not report is None (clients render N/A).
"""

import csv
import glob
import json
import os
import re
import shutil
import subprocess

NVIDIA_SMI = "nvidia-smi"
AMD_SMI = "amd-smi"
SMI_TIMEOUT = 10
DRM_CARD_GLOB = "/sys/class/drm/card*"

GPU_KEYS = (
    "index",
    "name",
    "uuid",
    "driver",
    "backend",
    "util",
    "mem_util",
    "mem_used",
    "mem_total",
    "temp",
    "power_w",
    "power_cap_w",
    "fan_pct",
    "clock_gfx",
    "clock_gfx_max",
    "clock_mem",
    "clock_mem_max",
    "pcie_gen",
    "pcie_gen_max",
    "pcie_width",
    "pcie_width_max",
    "pstate",
    "throttle",
)


def gpu_index():
    try:
        return max(0, int(os.environ.get("NVSTATS_GPU_INDEX", "0")))
    except ValueError:
        return 0


def empty_gpu(index=0, backend=None):
    g = dict.fromkeys(GPU_KEYS)
    g["index"] = index
    g["backend"] = backend
    return g


def _num(raw):
    """nvidia-smi CSV cell -> int/float, or None for N/A and friends."""
    s = raw.strip()
    if not s or s.startswith("[") or s in ("N/A", "Unknown Error"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return int(v) if v == int(v) else v


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path):
    s = _read(path)
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


# --- NVIDIA ---------------------------------------------------------------

_NV_FIELDS = (
    "index",
    "name",
    "uuid",
    "driver_version",
    "temperature.gpu",
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "fan.speed",
    "clocks.gr",
    "clocks.mem",
    "clocks.max.gr",
    "clocks.max.mem",
    "pcie.link.gen.current",
    "pcie.link.gen.max",
    "pcie.link.width.current",
    "pcie.link.width.max",
    "pstate",
)
# Drivers renamed clocks_throttle_reasons.* to clocks_event_reasons.*; the
# first prefix the installed driver accepts is remembered.
_NV_THROTTLE_PREFIXES = ("clocks_event_reasons", "clocks_throttle_reasons", None)
_NV_THROTTLE_SUFFIXES = (
    ("hw_slowdown", "HW Slowdown"),
    ("sw_power_cap", "SW Power Cap"),
    ("hw_thermal_slowdown", "HW Thermal"),
    ("sw_thermal_slowdown", "SW Thermal"),
)
_nv_prefix_i = 0


def parse_nvidia_row(line, with_throttle):
    """One CSV line of _NV_FIELDS (+ 4 throttle booleans) -> gpu dict."""
    cells = next(csv.reader([line], skipinitialspace=True))
    n = len(_NV_FIELDS)
    if len(cells) < n:
        return None
    row = dict(zip(_NV_FIELDS, (c.strip() for c in cells[:n])))
    throttle = "None"
    if with_throttle and len(cells) >= n + len(_NV_THROTTLE_SUFFIXES):
        active = [
            label
            for (_, label), cell in zip(_NV_THROTTLE_SUFFIXES, cells[n:])
            if cell.strip() == "Active"
        ]
        if active:
            throttle = ", ".join(active)
    g = empty_gpu(_num(row["index"]), "nvidia")
    g.update(
        name=row["name"] or None,
        uuid=row["uuid"] or None,
        driver=row["driver_version"] or None,
        temp=_num(row["temperature.gpu"]),
        util=_num(row["utilization.gpu"]),
        mem_util=_num(row["utilization.memory"]),
        mem_used=_num(row["memory.used"]),
        mem_total=_num(row["memory.total"]),
        power_w=_num(row["power.draw"]),
        power_cap_w=_num(row["power.limit"]),
        fan_pct=_num(row["fan.speed"]),
        clock_gfx=_num(row["clocks.gr"]),
        clock_gfx_max=_num(row["clocks.max.gr"]),
        clock_mem=_num(row["clocks.mem"]),
        clock_mem_max=_num(row["clocks.max.mem"]),
        pcie_gen=_num(row["pcie.link.gen.current"]),
        pcie_gen_max=_num(row["pcie.link.gen.max"]),
        pcie_width=_num(row["pcie.link.width.current"]),
        pcie_width_max=_num(row["pcie.link.width.max"]),
        pstate=row["pstate"] if row["pstate"].startswith("P") else None,
        throttle=throttle if with_throttle else None,
    )
    return g


def _nvidia_sample(idx):
    global _nv_prefix_i
    if not shutil.which(NVIDIA_SMI):
        return None
    while True:
        prefix = _NV_THROTTLE_PREFIXES[_nv_prefix_i]
        fields = list(_NV_FIELDS)
        if prefix:
            fields += [f"{prefix}.{s}" for s, _ in _NV_THROTTLE_SUFFIXES]
        try:
            r = subprocess.run(
                [
                    NVIDIA_SMI,
                    f"--id={idx}",
                    "--query-gpu=" + ",".join(fields),
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=SMI_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            if prefix and "valid field" in (r.stdout + r.stderr):
                _nv_prefix_i += 1
                continue
            return None
        lines = r.stdout.strip().splitlines()
        return parse_nvidia_row(lines[0], bool(prefix)) if lines else None


def nvidia_compute_procs(gpu_uuid=None):
    """[{pid, vram_mib, name}] of CUDA compute processes, optionally for one GPU.

    The proprietary NVIDIA driver exposes no DRM fdinfo, so this is the only
    per-process GPU memory source on NVIDIA hosts.
    """
    if not shutil.which(NVIDIA_SMI):
        return []
    try:
        out = subprocess.run(
            [
                NVIDIA_SMI,
                "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=SMI_TIMEOUT,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines():
        cells = next(csv.reader([line], skipinitialspace=True), [])
        if len(cells) < 4 or not cells[1].strip().isdigit():
            continue
        if gpu_uuid and cells[0].strip() != gpu_uuid:
            continue
        procs.append(
            {
                "pid": int(cells[1]),
                "name": cells[2].strip(),
                "vram_mib": _num(cells[3]),
            }
        )
    return procs


# --- AMD ------------------------------------------------------------------

_PCIE_GEN = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}
_amd_static_cache = {}


def _amd_run(args):
    out = subprocess.run(
        [AMD_SMI] + args + ["--json"],
        capture_output=True,
        text=True,
        timeout=SMI_TIMEOUT,
        check=True,
    ).stdout.strip()
    return json.loads(out) if out else None


def _val(node):
    """amd-smi {"value": x, "unit": u} (or bare scalar) -> number|None."""
    if isinstance(node, dict):
        node = node.get("value")
    if isinstance(node, bool) or node is None:
        return None
    try:
        # amd-smi reports some values as numeric strings ("2900"), others as
        # numbers, and unsupported ones as "N/A".
        v = float(node)
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def _max_level(levels):
    if not isinstance(levels, dict):
        return None
    vals = [v for v in (_val(lvl) for lvl in levels.values()) if v is not None]
    return max(vals) if vals else None


def _amd_row(data, idx):
    rows = data.get("gpu_data", []) if isinstance(data, dict) else data or []
    for row in rows:
        if isinstance(row, dict) and str(row.get("gpu")) == str(idx):
            return row
    return None


def _amd_static(idx):
    """Identity and ceilings never change while the daemon runs, and
    `amd-smi static` is ~50x the payload of a `metric` poll: fetch once."""
    if idx in _amd_static_cache:
        return _amd_static_cache[idx]
    s = {}
    if shutil.which(AMD_SMI):
        try:
            row = _amd_row(_amd_run(["static"]), idx) or {}
            asic = row.get("asic") or {}
            board = row.get("board") or {}
            bus = row.get("bus") or {}
            clock = row.get("clock") or {}
            limit = row.get("limit") or {}
            gen = bus.get("pcie_interface_version")
            gen = re.sub(r"\D", "", gen) if isinstance(gen, str) else None
            width = re.sub(r"\D", "", str(bus.get("max_pcie_width") or ""))
            s = {
                "name": asic.get("market_name") or board.get("product_name"),
                "driver": (row.get("driver") or {}).get("version"),
                "vram_type": (row.get("vram") or {}).get("type"),
                "power_cap_w": _val((limit.get("ppt0") or {}).get("socket_power_limit")),
                "clock_gfx_max": _max_level((clock.get("sys") or {}).get("frequency_levels")),
                "clock_mem_max": _max_level((clock.get("mem") or {}).get("frequency_levels")),
                "pcie_gen_max": int(gen) if gen else None,
                "pcie_width_max": int(width) if width else None,
            }
            for lrow in _amd_run(["list"]) or []:
                if str(lrow.get("gpu")) == str(idx):
                    s["uuid"] = lrow.get("uuid")
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            s = {}  # retried on the next sample
            return s
    _amd_static_cache[idx] = s
    return s


def _amd_device(idx):
    """sysfs device dir of the idx-th AMD GPU, or None."""
    devs = []
    for card in sorted(glob.glob(DRM_CARD_GLOB)):
        if not re.fullmatch(r"card\d+", os.path.basename(card)):
            continue
        dev = card + "/device"
        if _read(dev + "/vendor") == "0x1002":
            devs.append(dev)
    return devs[idx] if idx < len(devs) else None


MIB = 1024 * 1024
# Memory behind an APU's "VRAM" is a small carve-out of system RAM; the GTT
# pool is what the GPU can really use, so APUs report GTT and discrete cards
# report VRAM. amd-smi names the memory type; without it, fall back on size.
_SHARED_MEMORY_TYPES = ("DDR", "LPDDR")
_CARVEOUT_MAX = 4 * 1024 * MIB


def _amd_memory(dev, vram_type):
    vram = (_read_int(dev + "/mem_info_vram_used"), _read_int(dev + "/mem_info_vram_total"))
    gtt = (_read_int(dev + "/mem_info_gtt_used"), _read_int(dev + "/mem_info_gtt_total"))
    if vram_type:
        shared = vram_type.upper().startswith(_SHARED_MEMORY_TYPES)
    else:
        shared = vram[1] is not None and vram[1] <= _CARVEOUT_MAX
    used, total = gtt if shared and None not in gtt else vram
    if used is None or not total:
        return None, None
    return used // MIB, total // MIB


def _amd_sample(idx):
    dev = _amd_device(idx)
    if dev is None:
        return None
    g = empty_gpu(idx, "amd")
    static = _amd_static(idx)
    for k in ("name", "uuid", "driver", "clock_gfx_max", "clock_mem_max",
              "pcie_gen_max", "pcie_width_max", "power_cap_w"):
        g[k] = static.get(k)
    g["util"] = _read_int(dev + "/gpu_busy_percent")
    g["mem_used"], g["mem_total"] = _amd_memory(dev, static.get("vram_type"))
    if g["mem_used"] is not None:
        g["mem_util"] = round(g["mem_used"] * 100 / g["mem_total"], 1)
    hw = next(iter(sorted(glob.glob(dev + "/hwmon/hwmon*"))), None)
    if hw:
        t = _read_int(hw + "/temp1_input")
        g["temp"] = t / 1000.0 if t is not None else None
        pw = _read_int(hw + "/power1_average")
        if pw is None:
            pw = _read_int(hw + "/power1_input")
        g["power_w"] = round(pw / 1e6, 2) if pw is not None else None
        cap = _read_int(hw + "/power1_cap")
        if cap:
            g["power_cap_w"] = cap / 1e6
        f = _read_int(hw + "/freq1_input")
        g["clock_gfx"] = f // 1_000_000 if f is not None else None
        pwm, pmax = _read_int(hw + "/pwm1"), _read_int(hw + "/pwm1_max")
        g["fan_pct"] = round(pwm * 100 / pmax) if pwm is not None and pmax else None
    for line in (_read(dev + "/pp_dpm_mclk") or "").splitlines():
        m = re.search(r"(\d+)\s*Mhz\s*\*", line, re.I)
        if m:
            g["clock_mem"] = int(m.group(1))
    m = re.match(r"([\d.]+)\s*GT/s", _read(dev + "/current_link_speed") or "")
    if m:
        g["pcie_gen"] = _PCIE_GEN.get(float(m.group(1)))
    g["pcie_width"] = _read_int(dev + "/current_link_width")
    g["throttle"] = "None"
    return g


# --- dispatch -------------------------------------------------------------

_BACKENDS = {"nvidia": _nvidia_sample, "amd": _amd_sample}
_backend_cache = None


def gpu_sample():
    """The selected GPU as a dict with every GPU_KEYS entry."""
    global _backend_cache
    idx = gpu_index()
    forced = os.environ.get("NVSTATS_GPU_BACKEND", "").strip().lower()
    if forced in _BACKENDS:
        order = [forced]
    elif _backend_cache:
        order = [_backend_cache]
    else:
        order = ["nvidia", "amd"]
    for name in order:
        g = _BACKENDS[name](idx)
        if g:
            _backend_cache = name
            return g
    _backend_cache = None  # re-detect next time (e.g. driver reload)
    return empty_gpu(idx)
