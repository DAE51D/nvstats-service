#!/usr/bin/env python3
"""Shared GPU, system and LLM telemetry poller for nvstats-service.

Emits one JSON object per line on stdout, once per second (standalone use;
the services import these functions instead):

    {
      "gpu": {...},   # see nvstats_gpu.GPU_KEYS; unknown values are null
      "sys": {"ram_used": int, "ram_total": int, "swap_used": int,
              "swap_total": int, "load1": float, "load5": float, "load15": float,
              "cpu": float|None},
      "llm": {"llama": [ {...}, ... ], "ollama": [ {...}, ... ], "vllm": [ {...} ]},
      "gpu_procs": [ {pid, user, name, cmdline, argv, type: [...], gpu_pct, vram_kb}, ... ]
    }

GPU sampling lives in nvstats_gpu.py. Missing values are null (clients
render N/A).
"""

import json
import os
import pwd
import re
import subprocess
import sys
import time

from nvstats_gpu import gpu_sample, nvidia_compute_procs


_prev_cpu = None


def _cpu_sample():
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError:
        return None
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + vals[4]
    total = sum(vals)
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return None
    d_idle = idle - _prev_cpu[0]
    d_total = total - _prev_cpu[1]
    _prev_cpu = (idle, total)
    if d_total <= 0:
        return None
    return round((1.0 - d_idle / d_total) * 100.0, 1)


def sys_sample():
    mi = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                mi[k.strip()] = int(rest.strip().split()[0])  # kB
    except OSError:
        pass
    ram_total = mi.get("MemTotal", 0)
    ram_used = ram_total - mi.get("MemAvailable", ram_total)
    swap_total = mi.get("SwapTotal", 0)
    swap_used = swap_total - mi.get("SwapFree", 0)
    load1 = load5 = load15 = 0.0
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        load1, load5, load15 = (float(parts[0]), float(parts[1]), float(parts[2]))
    except (OSError, ValueError, IndexError):
        pass
    return {
        "ram_used": ram_used * 1024,
        "ram_total": ram_total * 1024,
        "swap_used": swap_used * 1024,
        "swap_total": swap_total * 1024,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cpu": _cpu_sample(),
    }


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").split("\0")
    except OSError:
        return None


def _proc_status(pid, keys):
    out = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if k.strip() in keys:
                    out[k.strip()] = rest.strip()
    except OSError:
        pass
    return out


def _find(name, argv):
    """Return argv value for --name or -name (if present), else None."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _classify_llm(argv):
    """Recognize common Linux model-serving launchers from their argv only.

    Keep this intentionally shallow: do not inspect containers, config files,
    or desktop-app APIs. The detected launcher must provide a model argument
    before it is shown as a model process.
    """
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    if exe in ("llama-server", "llama-cli", "llama-bench"):
        return "llama"
    if exe == "ollama":
        return "ollama"

    # Console scripts may appear as argv[0], or as argv[1] when Python is the
    # interpreter. Do not scan arbitrary arguments: an unrelated client may
    # contain an engine name in an option value.
    launchers = [exe]
    if (
        (exe == "python" or exe.startswith("python3"))
        and len(argv) > 1
        and not argv[1].startswith("-")
    ):
        launchers.append(os.path.basename(argv[1]))
    if any(name == "vllm" or name.startswith("vllm-") for name in launchers):
        return "vllm"
    if any(name == "sglang" or name.startswith("sglang-") for name in launchers):
        return "sglang"
    if "text-generation-launcher" in launchers:
        return "tgi"

    module = _find("-m", argv)
    if module and (module == "vllm" or module.startswith("vllm.")):
        return "vllm"
    if module and module in ("sglang.launch_server", "sglang.srt.server"):
        return "sglang"
    return None


def _first_positional_after(argv, command):
    try:
        index = argv.index(command) + 1
    except ValueError:
        return None
    if index < len(argv) and not argv[index].startswith("-"):
        return argv[index]
    return None


def _llm_model(engine, argv):
    if engine == "llama":
        return _find("--model", argv) or _find("-m", argv)
    if engine == "ollama":
        return None
    if engine == "vllm":
        return (
            _find("--model", argv)
            or _find("--model-path", argv)
            or _first_positional_after(argv, "serve")
        )
    if engine == "sglang":
        return _find("--model-path", argv) or _find("--model", argv)
    if engine == "tgi":
        return _find("--model-id", argv) or _find("--model", argv)
    return None


def _is_ollama_owned(argv):
    """Ollama bundles its own llama-server binary (e.g.
    /usr/local/lib/ollama/llama-server) and launches one per loaded model.
    Those are implementation detail of the ollama section, not a separate
    standalone llama.cpp instance the user started themselves — skip them
    here so they don't show up as a phantom duplicate "llama.cpp" card."""
    return "ollama" in argv[0].split("/")[:-1]


def llm_sample():
    llama, ollama, vllm, sglang, tgi = [], [], [], [], []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        argv = _proc_cmdline(pid)
        engine = _classify_llm(argv)
        if engine is None:
            continue
        if engine == "llama" and _is_ollama_owned(argv):
            continue
        st = _proc_status(pid, {"Name", "VmRSS", "Threads"})
        clean_argv = [a for a in argv if a]
        entry = {
            "pid": int(pid),
            "engine": engine,
            "name": st.get("Name", os.path.basename(argv[0])),
            "cmdline": " ".join(clean_argv),
            # Full argv (not just the flattened cmdline string) so the UI can
            # pair each flag with its own value unambiguously -- a value
            # containing spaces (e.g. a JSON flag argument) is one argv
            # element but would be impossible to re-split correctly out of
            # the joined "cmdline" string alone.
            "argv": clean_argv,
            "rss_kb": int(st["VmRSS"].split()[0])
            if "VmRSS" in st and st["VmRSS"].split()
            else None,
            "threads": int(st["Threads"]) if st.get("Threads", "").isdigit() else None,
            "cpu_pct": _proc_cpu_pct(pid),
        }
        gpu_stats = _fdinfo_gpu_stats(pid)
        entry["gpu_pct"] = gpu_stats["gpu_pct"] if gpu_stats else None
        entry["vram_kb"] = gpu_stats["vram_kb"] if gpu_stats else None
        # user/type so the config page's User/Type filters can apply to
        # these cards too, not just the generic gpu_procs list.
        entry["type"] = gpu_stats["types"] if gpu_stats else []
        entry["user"] = _username(pid) or "?"
        entry["unit"] = owning_unit(pid)
        if engine == "llama":
            entry["model"] = _llm_model(engine, argv)
            entry["ngl"] = _find("--n-gpu-layers", argv)
            entry["ctx"] = _find("--ctx-size", argv)
            entry["parallel"] = _find("--parallel", argv)
            entry["alias"] = _find("--alias", argv)
            entry["port"] = _find("--port", argv)
            llama.append(entry)
        elif engine == "ollama":
            entry["subcmd"] = argv[1] if len(argv) > 1 else None
            entry["port"] = _find("--port", argv)
            ollama.append(entry)
        elif engine == "vllm":
            entry["model"] = _llm_model(engine, argv)
            entry["port"] = _find("--port", argv)
            vllm.append(entry)
        elif engine == "sglang":
            entry["model"] = _llm_model(engine, argv)
            entry["port"] = _find("--port", argv)
            sglang.append(entry)
        elif engine == "tgi":
            entry["model"] = _llm_model(engine, argv)
            entry["port"] = _find("--port", argv)
            tgi.append(entry)
    return {
        "llama": llama,
        "ollama": ollama,
        "vllm": vllm,
        "sglang": sglang,
        "tgi": tgi,
    }


def ollama_ps():
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "3", "http://127.0.0.1:11434/api/ps"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        models = json.loads(out).get("models", [])
        return [
            {
                "name": m.get("name"),
                "model": m.get("model"),
                "size_vram": m.get("size_vram"),
                "size": m.get("size"),
                "quantization": (m.get("details") or {}).get("quantization_level"),
                "parameter_size": (m.get("details") or {}).get("parameter_size"),
                "expires_at": m.get("expires_at"),
            }
            for m in models
        ]
    except Exception:
        return None


_prev_gpu_engines = {}  # pid -> {"time": monotonic, "engines": {name: ns}}


def _fdinfo_engines(pid):
    """Aggregate DRM fdinfo (drm-engine-*/drm-memory-*) across all of this
    pid's open DRM client fds, or None if it has none (not a GPU client).

    /proc/<pid>/fdinfo/* is only readable by that pid's own owner (or root)
    -- unlike /proc/<pid>/cmdline, which is world-readable. Since this
    poller runs as an unprivileged user, GPU-using processes owned by a
    *different* user (e.g. a service running under its own system account)
    are invisible to this generic scan and silently skipped, even though
    the same process would still show up in llm_sample() above (which only
    needs the world-readable cmdline). This is a real limitation, not a
    bug -- there's no way around it without running privileged."""
    fd_dir = f"/proc/{pid}/fdinfo"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    engines, mem, found = {}, {}, False
    seen_clients = set()
    for fd in fds:
        try:
            with open(f"{fd_dir}/{fd}") as f:
                content = f.read()
        except OSError:
            continue
        if "drm-driver" not in content:
            continue
        # A process can have multiple DRM fds referring to the same client.
        # fdinfo reports the client's complete allocation/counters on each
        # such fd, so summing them would double-count memory and engine time
        # (llama-server commonly has two amdgpu fds).  Deduplicate by the
        # kernel DRM client id; fall back to the fd when older drivers omit it.
        client_id = next(
            (
                line.split(":", 1)[1].strip()
                for line in content.splitlines()
                if line.startswith("drm-client-id:")
            ),
            fd,
        )
        if client_id in seen_clients:
            continue
        seen_clients.add(client_id)
        found = True
        for line in content.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip().split()
            if not v:
                continue
            if k.startswith("drm-engine-"):
                try:
                    engines[k[len("drm-engine-") :]] = engines.get(
                        k[len("drm-engine-") :], 0
                    ) + int(v[0])
                except ValueError:
                    pass
            elif k.startswith("drm-memory-"):
                try:
                    mem[k[len("drm-memory-") :]] = mem.get(
                        k[len("drm-memory-") :], 0
                    ) + int(v[0])
                except ValueError:
                    pass
    return {"engines": engines, "mem": mem} if found else None


def _gpu_engine_pct(pid, engines):
    """% busy per engine since the last sample (nvtop's own approach:
    delta of the cumulative ns counter / elapsed wall time). None on a
    process's first-ever sample (no baseline yet), matching _cpu_sample()."""
    now = time.monotonic()
    prev = _prev_gpu_engines.get(pid)
    _prev_gpu_engines[pid] = {"time": now, "engines": dict(engines)}
    if prev is None:
        return None
    dt = now - prev["time"]
    if dt <= 0:
        return None
    pct = {}
    for name, ns in engines.items():
        d_ns = max(0, ns - prev["engines"].get(name, ns))
        pct[name] = round(min(100.0, d_ns / (dt * 1e9) * 100.0), 1)
    return pct


def _username(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    try:
                        return pwd.getpwuid(uid).pw_name
                    except KeyError:
                        return str(uid)  # no passwd entry (e.g. container uid)
    except (OSError, ValueError):
        pass
    return None


# Units whose restart would take the desktop session or this monitor down.
_UNIT_DENY = re.compile(
    r"^(sddm|gdm|lightdm|display-manager|systemd-logind|dbus|nvstats)\b"
    r"|kwin|^user@|^user-runtime-dir@|^session-"
)
_UNIT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@\-]*\.service$")


def owning_unit(pid):
    """The systemd *service* that owns pid, as "system:<name>" or
    "user:<name>", or None when pid isn't in a restartable service (desktop
    app scopes, another user's session units, anything on the deny list).

    Read from /proc/<pid>/cgroup, which is world-readable, so this needs no
    privileges. The name is validated so it is safe to hand to systemctl.
    """
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            path = next(
                (line.split(":", 2)[2].strip() for line in f if line.startswith("0::")),
                "",
            )
    except (OSError, IndexError):
        return None
    comps = [c for c in path.split("/") if c]
    scope = name = None
    if comps and comps[0] == "system.slice":
        scope = "system"
        name = next((c for c in comps[1:] if c.endswith(".service")), None)
    else:
        # .../user@<uid>.service/<slice>/<unit>.service -- the unit is the
        # first .service *after* this user's manager, and only our own
        # user's units are ours to restart.
        manager = f"user@{os.getuid()}.service"
        if manager in comps:
            scope = "user"
            rest = comps[comps.index(manager) + 1 :]
            name = next((c for c in rest if c.endswith(".service")), None)
    if not name or not _UNIT_NAME.match(name) or _UNIT_DENY.search(name):
        return None
    return f"{scope}:{name}"


def _fdinfo_gpu_stats(pid):
    """{'gpu_pct': float|None, 'vram_kb': int|None, 'types': [...]} for one
    pid, or None if it has no open DRM client fd (not a GPU client).
    Shared by gpu_proc_sample() (every GPU process) and llm_sample() (so
    llama.cpp/ollama/vllm cards can show their own live GPU load/memory,
    not just the system-wide totals)."""
    fdinfo = _fdinfo_engines(pid)
    if fdinfo is None:
        return None
    # A DRM client fd with no drm-engine-* key yet is still a real GPU
    # client (nvtop shows these too, e.g. systemd-logind/init holding a
    # DRM master fd for VT handoff with no measurable engine time) --
    # only "no DRM fd at all" should exclude a process, not "idle so far".
    pct = _gpu_engine_pct(int(pid), fdinfo["engines"]) or {}
    types = []
    if "gfx" in fdinfo["engines"]:
        types.append("graphic")
    if "compute" in fdinfo["engines"]:
        types.append("compute")
    if not types:
        types.append("other")  # decode/encode-only (e.g. video playback)
    return {
        "gpu_pct": round(sum(pct.values()), 1) if pct else None,
        "vram_kb": sum(fdinfo["mem"].values()) or None,
        "types": types,
    }


_prev_proc_cpu = {}  # pid -> {"time": monotonic, "ticks": utime+stime}


def _proc_cpu_pct(pid):
    """Per-process CPU%, same delta-of-a-cumulative-counter approach as
    _cpu_sample()/_gpu_engine_pct() -- None on a process's first sample."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            content = f.read()
    except OSError:
        return None
    # comm (field 2) is parenthesized and can itself contain spaces/parens,
    # so split after the LAST ')' rather than just splitting on whitespace.
    rest = content.rsplit(")", 1)[-1].split()
    try:
        ticks = int(rest[11]) + int(rest[12])  # utime + stime
    except (IndexError, ValueError):
        return None
    now = time.monotonic()
    key = int(pid)
    prev = _prev_proc_cpu.get(key)
    _prev_proc_cpu[key] = {"time": now, "ticks": ticks}
    if prev is None:
        return None
    dt = now - prev["time"]
    if dt <= 0:
        return None
    hz = os.sysconf("SC_CLK_TCK")
    d_ticks = max(0, ticks - prev["ticks"])
    # Multi-threaded processes can exceed 100% (e.g. 2 full cores = 200%),
    # same convention as `top`/`ps` -- just sanity-clamp to the core count.
    return round(min(100.0 * os.cpu_count(), d_ticks / hz / dt * 100.0), 1)


NONCLIENT_RECHECK = 10  # ticks between fd scans of known non-GPU processes
_nonclient_pids = set()
_scan_tick = 0


def gpu_proc_sample(gpu_uuid=None, backend=None):
    """Every process using the GPU -- the generic, nvtop-style process list
    (as opposed to llm_sample()'s LLM-only view).

    Sources: open DRM client fds (AMD/Intel/nouveau; see _fdinfo_engines() for
    the same-user visibility caveat) plus, on NVIDIA, `nvidia-smi
    --query-compute-apps`, because the proprietary driver exposes no fdinfo.
    """
    global _scan_tick
    procs = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        pids = []
    _scan_tick += 1
    alive = set()
    for pid_s in pids:
        pid = int(pid_s)
        # Reading every fd of every process is the single most expensive thing
        # this daemon does. A process that is not a GPU client rarely becomes
        # one, so re-check those only every NONCLIENT_RECHECK ticks (staggered
        # by pid); known clients are read every tick.
        if pid in _nonclient_pids and (pid + _scan_tick) % NONCLIENT_RECHECK:
            continue
        stats = _fdinfo_gpu_stats(pid_s)
        if stats is None:
            _nonclient_pids.add(pid)
            continue
        _nonclient_pids.discard(pid)
        argv = [a for a in (_proc_cmdline(pid_s) or []) if a]
        if not argv:
            continue
        alive.add(int(pid_s))
        # Name from /proc/status, not argv[0] -- some processes (observed:
        # Chromium/Electron GPU helpers) rewrite their own argv into one
        # long descriptive blob for `ps` display, which makes a poor "name".
        st = _proc_status(pid_s, {"Name", "VmRSS"})
        name = st.get("Name") or os.path.basename(argv[0])
        procs.append(_proc_entry(pid_s, name, argv, st, stats))
    if backend == "nvidia":
        for app in nvidia_compute_procs(gpu_uuid):
            pid_s = str(app["pid"])
            if app["pid"] in alive:
                continue
            alive.add(app["pid"])
            argv = [a for a in (_proc_cmdline(pid_s) or []) if a]
            st = _proc_status(pid_s, {"Name", "VmRSS"})
            name = st.get("Name") or os.path.basename(app["name"])
            vram = app["vram_mib"]
            stats = {
                "types": ["compute"],
                "gpu_pct": None,
                "vram_kb": int(vram * 1024) if vram is not None else None,
            }
            procs.append(_proc_entry(pid_s, name, argv or [app["name"]], st, stats))
    # Drop tracking state for pids that no longer exist so these don't grow
    # without bound across a long-running poller session.
    for tracked in list(_prev_gpu_engines.keys()):
        if tracked not in alive:
            del _prev_gpu_engines[tracked]
    _nonclient_pids.intersection_update(int(p) for p in pids)
    return procs


def _proc_entry(pid_s, name, argv, st, stats):
    return {
        "pid": int(pid_s),
        "user": _username(pid_s) or "?",
        "unit": owning_unit(pid_s),
        "name": name,
        "cmdline": " ".join(argv),
        "argv": argv,
        "type": stats["types"],
        "gpu_pct": stats["gpu_pct"],
        "vram_kb": stats["vram_kb"],
        "cpu_pct": _proc_cpu_pct(pid_s),
        "rss_kb": int(st["VmRSS"].split()[0])
        if "VmRSS" in st and st["VmRSS"].split()
        else None,
    }


def main():
    while True:
        try:
            llm = llm_sample()
            llm["ollama_models"] = ollama_ps() or []
            gpu = gpu_sample()
            sample = {
                "gpu": gpu,
                "sys": sys_sample(),
                "llm": llm,
                "gpu_procs": gpu_proc_sample(gpu.get("uuid"), gpu.get("backend")),
            }
        except Exception:
            sample = {
                "gpu": {},
                "sys": {},
                "llm": {"llama": [], "ollama": [], "vllm": [], "ollama_models": []},
                "gpu_procs": [],
            }
        try:
            sys.stdout.write(json.dumps(sample) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return
        time.sleep(1.0)


if __name__ == "__main__":
    main()
