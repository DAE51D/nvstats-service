"""Optional NPU telemetry for nvstats-service.

Discovery is automatic and the whole feature is invisible on hosts without an
NPU: `NpuSampler.sample()` returns ``None`` and the sample has ``"npu": null``.

Supported: AMD XDNA / XDNA2 (``amdxdna`` driver, Ryzen AI). Any other accel
device that is found (e.g. Intel ``intel_vpu``) is reported as present but not
readable, so a client can show N/A and the chip can be diagnosed later.

What the amdxdna driver does NOT provide (checked on kernel 7.0, Strix Halo):
a hardware busy counter. The sensors ioctl answers ENODEV, debugfs is root
only, and fdinfo carries no ``drm-engine-*`` time. What it does provide are
the hardware-context counters (commands submitted/completed per context), so
``util`` is an *activity* estimate: the share of the last ~2 s in which any
command was in flight or completed. It is 0 when idle and ~100 while a model
is generating, but it is not a precise busy-time figure; ``util_source`` says
so, and clients should label it accordingly. If a future driver exposes real
utilisation, only this module changes.

Memory is not independent: the NPU allocates from the same unified system
RAM as the CPU and iGPU on these APUs, so ``sys.ram_used`` already includes it.
``mem_used`` here is only the NPU's own device-heap usage, when the driver
reports it (``null`` otherwise).

Everything is read-only, stdlib only, and runs unprivileged (the device node
needs the `render` group).
"""

import ctypes
import errno
import fcntl
import os
import struct
import time

from nvstats_poll import _proc_cmdline, _proc_status, _username, owning_unit

SYSFS_ACCEL = "/sys/class/accel"
SUPPORTED_DRIVERS = ("amdxdna",)
RECHECK_ABSENT_SECONDS = 60.0  # how often to look again when no NPU was found
UTIL_WINDOW_SECONDS = 2.0

# --- amdxdna UAPI (include/uapi/drm/amdxdna_accel.h) -------------------------
_IOC_WRITE, _IOC_READ = 1, 2
_DRM_COMMAND_BASE = 0x40


def _iowr(nr, size):
    return ((_IOC_READ | _IOC_WRITE) << 30) | (size << 16) | (ord("d") << 8) | nr


_GET_INFO_FMT = struct.Struct("=IIQ")  # param, buffer_size, buffer
_GET_ARRAY_FMT = struct.Struct(
    "=IIIIQ"
)  # param, element_size, num_element, pad, buffer
_IOCTL_GET_INFO = _iowr(_DRM_COMMAND_BASE + 7, _GET_INFO_FMT.size)
_IOCTL_GET_ARRAY = _iowr(_DRM_COMMAND_BASE + 10, _GET_ARRAY_FMT.size)

_PARAM_CLOCK_METADATA = 3
_PARAM_RESOURCE_INFO = 12
_ARRAY_HWCTX_ALL = 0

_RESOURCE_FMT = struct.Struct(
    "=5Q"
)  # clk_max, tops_max, task_max, tops_curr, task_curr
# context_id, start_col, num_col, hwctx_id, pid, then 8 x u64 counters, then 14 x u32
_HWCTX_FMT = struct.Struct("=IIIIq8Q14I")
_HWCTX_KEYS = (
    "context_id",
    "start_col",
    "num_col",
    "hwctx_id",
    "pid",
    "submissions",
    "completions",
    "migrations",
    "preemptions",
    "errors",
    "priority",
    "heap_usage",
    "suspensions",
)
MAX_CONTEXTS = 64


class NpuError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason  # "permission" | "unsupported" | "io"
        self.detail = detail


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def discover(sysfs=SYSFS_ACCEL):
    """List accel devices: dicts with node, pci, driver, name, fw. [] if none."""
    try:
        names = sorted(n for n in os.listdir(sysfs) if n.startswith("accel"))
    except OSError:
        return []
    found = []
    for n in names:
        dev = os.path.join(sysfs, n, "device")
        try:
            driver = os.path.basename(os.readlink(os.path.join(dev, "driver")))
        except OSError:
            driver = None
        try:
            pci = os.path.basename(os.path.realpath(dev))
        except OSError:
            pci = None
        found.append(
            {
                "node": "/dev/accel/" + n,
                "pci": pci,
                "driver": driver,
                "name": _read(os.path.join(dev, "vbnv")) or driver or n,
                "fw": _read(os.path.join(dev, "fw_version")),
            }
        )
    return found


def _get_info(fd, param, size):
    buf = ctypes.create_string_buffer(size)
    req = bytearray(_GET_INFO_FMT.pack(param, size, ctypes.addressof(buf)))
    try:
        fcntl.ioctl(fd, _IOCTL_GET_INFO, req, True)
    except OSError as e:
        raise _classify(e)
    used = _GET_INFO_FMT.unpack(bytes(req))[1]
    return buf.raw[: min(used, size) or size]


def _get_hwctx(fd):
    esize = _HWCTX_FMT.size
    buf = ctypes.create_string_buffer(esize * MAX_CONTEXTS)
    req = bytearray(
        _GET_ARRAY_FMT.pack(
            _ARRAY_HWCTX_ALL, esize, MAX_CONTEXTS, 0, ctypes.addressof(buf)
        )
    )
    try:
        fcntl.ioctl(fd, _IOCTL_GET_ARRAY, req, True)
    except OSError as e:
        raise _classify(e)
    count = min(_GET_ARRAY_FMT.unpack(bytes(req))[2], MAX_CONTEXTS)
    out = []
    for i in range(count):
        vals = _HWCTX_FMT.unpack_from(buf.raw, i * esize)
        out.append(dict(zip(_HWCTX_KEYS, vals)))
    return out


def _classify(e):
    if e.errno in (errno.EACCES, errno.EPERM):
        return NpuError("permission", "no access to the NPU device (render group?)")
    if e.errno in (errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP, errno.ENODEV):
        return NpuError("unsupported", "driver does not expose this query")
    return NpuError("io", os.strerror(e.errno))


class NpuSampler:
    """Holds the NPU handle and the rolling state behind `util`."""

    def __init__(self, sysfs=SYSFS_ACCEL, now=time.monotonic):
        self._sysfs = sysfs
        self._now = now
        self._dev = None
        self._fd = None
        self._checked_at = None
        self._history = []  # (monotonic, busy 0/1) for the device
        self._pid_history = {}  # pid -> [(monotonic, busy)]
        self._prev = {}  # context_id -> completions, to see progress
        self._info = {}
        self._info_at = 0.0
        self._last_t = None
        self._last_elapsed = None

    # -- discovery ---------------------------------------------------------
    def _discover(self):
        now = self._now()
        if (
            self._dev is None
            and self._checked_at is not None
            and now - self._checked_at < RECHECK_ABSENT_SECONDS
        ):
            return
        self._checked_at = now
        devs = discover(self._sysfs)
        # Prefer a device we can actually read.
        devs.sort(key=lambda d: d["driver"] not in SUPPORTED_DRIVERS)
        self._dev = devs[0] if devs else None

    def _open(self):
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self._dev["node"], os.O_RDWR)
        except OSError as e:
            raise _classify(e)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    # -- sampling ----------------------------------------------------------
    def sample(self):
        """(npu dict | None, [process dicts]). None means "no NPU here"."""
        self._discover()
        dev = self._dev
        if dev is None:
            return None, []
        base = {
            "present": True,
            "name": dev["name"],
            "driver": dev["driver"],
            "fw": dev["fw"],
            "pci": dev["pci"],
            "readable": False,
            "reason": None,
            "util": None,
            "util_source": None,
            "tasks": None,
            "tasks_max": None,
            "clock_mhz": None,
            "clock_max_mhz": None,
            "tops": None,
            "tops_max": None,
            "power_w": None,
            "mem_used": None,
            "cmds_per_s": None,
        }
        if dev["driver"] not in SUPPORTED_DRIVERS:
            base["reason"] = "unsupported driver: %s" % (dev["driver"] or "none")
            return base, []
        try:
            self._open()
            ctxs = _get_hwctx(self._fd)
        except NpuError as e:
            self.close()
            base["reason"] = e.reason + (": " + e.detail if e.detail else "")
            return base, []
        now = self._now()
        self._refresh_info(base)
        base["readable"] = True
        base["util_source"] = "activity"
        base["tasks"] = len(ctxs)
        base["mem_used"] = sum(c["heap_usage"] for c in ctxs) or None
        procs, completed = self._account(ctxs, now)
        busy = any(c["submissions"] > c["completions"] for c in ctxs) or completed > 0
        base["util"] = self._util(self._history, now, busy)
        elapsed = self._last_elapsed
        base["cmds_per_s"] = round(completed / elapsed, 1) if elapsed else None
        for p in procs:
            p["gpu_pct"] = self._util(
                self._pid_history.setdefault(p["pid"], []), now, p.pop("_busy")
            )
        live = {p["pid"] for p in procs}
        for pid in [k for k in self._pid_history if k not in live]:
            del self._pid_history[pid]
        return base, procs

    def _refresh_info(self, base):
        # Clocks and resource info barely change; read them every few seconds.
        now = self._now()
        if now - self._info_at > 5.0 or not self._info:
            info = {}
            try:
                r = _RESOURCE_FMT.unpack(
                    _get_info(self._fd, _PARAM_RESOURCE_INFO, _RESOURCE_FMT.size)
                )
                info["clock_max_mhz"] = r[0]
                info["tasks_max"] = r[2]
                info["tops"], info["tops_max"] = r[3], r[1]
            except (NpuError, struct.error):
                pass
            try:
                raw = _get_info(self._fd, _PARAM_CLOCK_METADATA, 48)
                # {mp_npu: char name[16]; u32 mhz; u32 pad}, {h: same}
                info["clock_mhz"] = struct.unpack_from("=I", raw, 40)[0]  # H clock
            except (NpuError, struct.error):
                pass
            self._info, self._info_at = info, now
        base.update(self._info)

    def _account(self, ctxs, now):
        """Per-pid rows + total commands completed since the last sample."""
        elapsed = None if self._last_t is None else now - self._last_t
        self._last_t = now
        self._last_elapsed = elapsed
        completed_total = 0
        by_pid = {}
        seen = set()
        for c in ctxs:
            seen.add(c["context_id"])
            prev = self._prev.get(c["context_id"])
            delta = max(0, c["completions"] - prev) if prev is not None else 0
            self._prev[c["context_id"]] = c["completions"]
            completed_total += delta
            row = by_pid.setdefault(
                c["pid"], {"ctxs": 0, "inflight": 0, "delta": 0, "heap": 0, "errors": 0}
            )
            row["ctxs"] += 1
            row["inflight"] += max(0, c["submissions"] - c["completions"])
            row["delta"] += delta
            row["heap"] += c["heap_usage"]
            row["errors"] += c["errors"]
        for cid in [k for k in self._prev if k not in seen]:
            del self._prev[cid]
        procs = []
        for pid, row in sorted(by_pid.items()):
            spid = str(pid)
            argv = [a for a in (_proc_cmdline(spid) or []) if a]
            st = _proc_status(spid, {"Name", "VmRSS"})
            name = st.get("Name") or (os.path.basename(argv[0]) if argv else spid)
            procs.append(
                {
                    "pid": pid,
                    "user": _username(spid) or "?",
                    "unit": owning_unit(spid),
                    "name": name,
                    "cmdline": " ".join(argv),
                    "argv": argv,
                    "type": ["npu"],
                    "device": "npu",
                    "gpu_pct": None,
                    "vram_kb": (row["heap"] // 1024) or None,
                    "cpu_pct": None,
                    "rss_kb": int(st["VmRSS"].split()[0])
                    if st.get("VmRSS", "").split()
                    else None,
                    "npu_ctxs": row["ctxs"],
                    "_busy": row["inflight"] > 0 or row["delta"] > 0,
                }
            )
        return procs, completed_total

    @staticmethod
    def _util(history, now, busy):
        history.append((now, 1 if busy else 0))
        cutoff = now - UTIL_WINDOW_SECONDS
        while len(history) > 1 and history[0][0] < cutoff:
            history.pop(0)
        return round(100.0 * sum(b for _, b in history) / len(history), 1)
