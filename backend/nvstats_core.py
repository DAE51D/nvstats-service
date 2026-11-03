"""Transport-independent core of nvstats-service.

The Sampler polls once a second in a background thread and publishes an
immutable sample. The D-Bus and HTTP front-ends (service/) only read from it,
so neither needs the other's dependencies (HTTP needs no dbus/GLib).

Sample schema (version 1). Meta: schema, host, seq, ts, version, git_commit.
Payload: gpu, sys, llm, gpu_procs (+ current_user, can_kill_all when the
Sampler was built with detect_kill=True). Two views:

* full    -- everything above.
* minimal -- meta + gpu + the top few gpu_procs, trimmed to what a small
             display needs (no argv arrays, no llm/sys).
"""

import logging
import os
import re
import socket
import subprocess
import threading
import time

from nvstats_gpu import gpu_sample
from nvstats_poll import gpu_proc_sample, llm_sample, ollama_ps, sys_sample

VERSION = "0.2.1"
SCHEMA = 1
# Stamped by install.sh from `git rev-parse --short HEAD` (-dirty suffix when
# the tree differs from HEAD), so clients can show what is actually running.
GIT_COMMIT = "unknown"

POLL_SECONDS = 1.0
MINIMAL_PROCS = 5
# Some processes (browsers, ffmpeg filter graphs) have multi-KB command lines;
# the minimal view targets a microcontroller with little RAM.
MINIMAL_CMDLINE_MAX = 1024

log = logging.getLogger("nvstats")


def _current_user():
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError):
        return os.environ.get("USER", "")


def _can_kill_all():
    """Passwordless sudo lets Kill reach other users' processes."""
    try:
        r = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _attach_gpu_to_llm(llm, procs):
    """Give LLM cards the per-process GPU numbers from gpu_procs when their
    own fdinfo read found none (always the case on NVIDIA)."""
    by_pid = {p["pid"]: p for p in procs}
    for key, entries in llm.items():
        if key == "ollama_models":
            continue
        for e in entries:
            p = by_pid.get(e.get("pid"))
            if not p:
                continue
            for k in ("gpu_pct", "vram_kb"):
                if e.get(k) is None and p.get(k) is not None:
                    e[k] = p[k]
            if not e.get("type"):
                e["type"] = p.get("type", [])


class Sampler:
    def __init__(self, detect_kill=False):
        self._detect_kill = detect_kill
        self._cond = threading.Condition()
        self._latest = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread = None
        self._host = socket.gethostname()

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._stop.clear()

    def _sample_once(self, extra):
        llm = llm_sample()
        llm["ollama_models"] = ollama_ps() or []
        gpu = gpu_sample()
        procs = gpu_proc_sample(gpu.get("uuid"), gpu.get("backend"))
        _attach_gpu_to_llm(llm, procs)
        return {
            "gpu": gpu,
            "sys": sys_sample(),
            "llm": llm,
            "gpu_procs": procs,
            **extra,
        }

    def _poll_loop(self):
        extra = {}
        if self._detect_kill:
            # Same-user processes are always killable; anything else needs
            # passwordless sudo, detected once at startup.
            extra = {"current_user": _current_user(), "can_kill_all": _can_kill_all()}
            log.info("can_kill_all=%s", extra["can_kill_all"])
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                sample = self._sample_once(extra)
            except Exception:
                log.exception("sampling failed")
                sample = None
            if sample is not None:
                with self._cond:
                    self._seq += 1
                    sample.update(
                        schema=SCHEMA,
                        host=self._host,
                        seq=self._seq,
                        ts=time.time(),
                        version=VERSION,
                        git_commit=GIT_COMMIT,
                    )
                    self._latest = sample
                    self._cond.notify_all()
            self._stop.wait(max(0.0, POLL_SECONDS - (time.monotonic() - started)))

    def latest(self):
        """The newest published sample (never mutated afterwards), or None
        before the first poll completes."""
        with self._cond:
            return self._latest

    def wait_newer(self, seq, timeout):
        """Block until a sample newer than seq exists; None on timeout."""
        with self._cond:
            self._cond.wait_for(
                lambda: (self._latest is not None and self._latest["seq"] > seq)
                or self._stop.is_set(),
                timeout,
            )
            if self._latest is not None and self._latest["seq"] > seq:
                return self._latest
            return None


# --- views ------------------------------------------------------------------

_SECRET_FLAG = re.compile(r"key|token|secret|passw|auth|credential", re.I)


def redact_argv(argv):
    """Mask the value of options that look like they carry a secret
    (--api-key X, --token=X, ...). Everything else stays readable."""
    out, mask_next = [], False
    for a in argv:
        if mask_next and not a.startswith("-"):
            out.append("***")
            mask_next = False
            continue
        mask_next = False
        if a.startswith("-"):
            name, eq, _ = a.partition("=")
            if _SECRET_FLAG.search(name):
                if eq:
                    out.append(name + "=***")
                else:
                    out.append(a)
                    mask_next = True
                continue
        out.append(a)
    return out


def _redacted(entry):
    e = dict(entry)
    if e.get("argv"):
        e["argv"] = redact_argv(e["argv"])
        e["cmdline"] = " ".join(e["argv"])
    return e


META_KEYS = ("schema", "host", "seq", "ts", "version", "git_commit")


def view_full(sample, redact=False):
    if not redact:
        return sample
    out = dict(sample)
    out["gpu_procs"] = [_redacted(p) for p in sample["gpu_procs"]]
    out["llm"] = {
        k: (v if k == "ollama_models" else [_redacted(e) for e in v])
        for k, v in sample["llm"].items()
    }
    return out


def view_minimal(sample, redact=True, max_procs=MINIMAL_PROCS):
    procs = sorted(
        sample["gpu_procs"], key=lambda p: p.get("vram_kb") or 0, reverse=True
    )[:max_procs]
    out = {k: sample[k] for k in META_KEYS}
    out["gpu"] = sample["gpu"]
    out["sys"] = {"cpu": (sample.get("sys") or {}).get("cpu")}
    out["gpu_procs"] = [
        {
            "pid": p["pid"],
            "name": p["name"],
            "user": p["user"],
            "vram_kb": p["vram_kb"],
            "gpu_pct": p["gpu_pct"],
            "cpu_pct": p["cpu_pct"],
            "cmdline": " ".join(redact_argv(p["argv"]) if redact else p["argv"])[
                :MINIMAL_CMDLINE_MAX
            ],
        }
        for p in procs
    ]
    return out
