# nvstats-service

The standalone, per-user D-Bus telemetry daemon shared by GPU monitoring clients. It polls GPU/system/process data once and publishes the latest sample on the current user's session bus. The Plasma knvtop widget and OpenChamber onvtop extension are independent clients; neither contains its own sampler.

For an AI-assisted setup, point your coding agent at the [install skill](SKILL.md). Review package-manager prompts and optional privilege changes before approving them.

## APIs

**D-Bus** (per-user session bus, for knvtop/onvtop on the same machine) uses the neutral `com.daevid.nvstats` bus name, `/com/daevid/nvstats` object path, and `com.daevid.nvstats.*` interfaces:

- `Sample.Get()` returns the latest sample as a JSON string (schema below).
- `Status.IsAlive()` and `Restart()` manage the poller.
- `Process.Kill()` and `Restart()` provide process controls.

**HTTP** (optional, read-only, stdlib only; for the Vobot Mini Dock `nvtop` app and, later, remote clients):

| Endpoint | |
| --- | --- |
| `GET /health` | liveness, version, never needs the token |
| `GET /api/v1/sample` | latest sample, **minimal** view: meta + `gpu` + top 5 `gpu_procs`; a few KB, sized for microcontrollers |
| `GET /api/v1/sample?full=1` | the full sample (`sys`, `llm`, argv arrays, ...) |
| `GET /api/v1/stream[?full=1]` | Server-Sent Events: one `sample` event per new sample (1 Hz), keep-alive comments in between |

Process control is **not** available over HTTP.

Sample schema (v1): meta `schema, host, seq, ts, version, git_commit`; `gpu` (one GPU, chosen with `NVSTATS_GPU_INDEX`, default 0) with `index, name, uuid, driver, backend, util, mem_util, mem_used, mem_total` (MiB), `temp, power_w, power_cap_w, fan_pct, clock_gfx, clock_gfx_max, clock_mem, clock_mem_max, pcie_gen, pcie_gen_max, pcie_width, pcie_width_max, pstate, throttle` (anything the hardware does not report is `null`); `gpu_procs[]` (`pid, name, user, vram_kb, gpu_pct, cpu_pct, cmdline`; `argv` etc. in the full view); `sys`, `llm` (llama.cpp, Ollama, vLLM, SGLang, TGI) in the full view. `seq` increases with every sample, so a client can detect a stale server.

GPU data comes from `nvidia-smi` (NVIDIA) or sysfs plus a one-time `amd-smi static` (AMD); per-process GPU use comes from DRM fdinfo, plus `nvidia-smi --query-compute-apps` on NVIDIA, whose driver exposes no fdinfo. Set `NVSTATS_GPU_BACKEND=nvidia|amd` to force one on a host that has both.

The clients in this release use these APIs directly; there is no legacy bus-name alias.

## Install

Two modes. Both remove the old `vobot-gpu-daemon` if it is installed (`--keep-legacy` to leave it, e.g. while it still owns port 8039).

**Desktop (user session, D-Bus)** - do not run with `sudo`. Needs Python 3 with D-Bus and GLib bindings (Debian/Ubuntu/Kubuntu: `sudo apt install python3-dbus python3-gi`):

```sh
git clone https://github.com/DAE51D/nvstats-service.git
cd nvstats-service
./install.sh                                   # D-Bus only
./install.sh --http                            # + HTTP on 127.0.0.1:8039
./install.sh --http --bind 0.0.0.0 --token S3CRET   # + reachable from the LAN
```

**Headless host (system service, HTTP only - no D-Bus, no desktop session needed)**:

```sh
sudo ./install.sh --system [--bind 0.0.0.0] [--port 8039] [--token S3CRET]
```

Settings live in an env file created once and never overwritten unless you pass the matching option (`~/.config/nvstats/nvstats.env`, or `/etc/nvstats/nvstats.env` for `--system`):

| Variable | Default | |
| --- | --- | --- |
| `NVSTATS_HTTP` | off (user mode) | `1` serves HTTP next to D-Bus |
| `NVSTATS_HTTP_BIND` | `127.0.0.1` (`0.0.0.0` with `--system`) | listen address |
| `NVSTATS_HTTP_PORT` | `8039` | |
| `NVSTATS_TOKEN` | unset | if set, `/api/v1/*` require `Authorization: Bearer <token>` |
| `NVSTATS_REDACT_ARGS` | `1` | mask values of secret-looking options (`--api-key`, `--token`, ...) in HTTP responses |
| `NVSTATS_GPU_INDEX` | `0` | which GPU to report |
| `NVSTATS_GPU_BACKEND` | auto | `nvidia` or `amd` |

Verify:

```sh
systemctl --user status nvstats.service        # or: systemctl status nvstats-httpd.service
busctl --user call com.daevid.nvstats /com/daevid/nvstats com.daevid.nvstats.Sample Get
curl http://localhost:8039/health
```

GPU tools are optional: `amd-smi`/`nvidia-smi` are used when available (NVIDIA needs `nvidia-smi`; AMD works from sysfs alone but names the card from `amd-smi`).

## Optional cross-user GPU process data

The daemon can read per-process GPU engine/memory details only for processes owned by the daemon user unless its interpreter has `CAP_SYS_PTRACE`. This is optional; GPU-wide and system-wide telemetry still works without it. To grant the capability only to a dedicated Python copy, follow the cross-user GPU stats instructions in the [knvtop documentation](https://github.com/DAE51D/knvtop#cross-user-gpu-stats-per-process-gpuvram-showing-na). Do not set the capability on the system-wide Python interpreter.

## Development checks

```sh
python3 -m py_compile service/*.py backend/*.py
python3 -m unittest discover -s tests
bash -n install.sh
dbus-run-session -- python3 scripts/smoke_dbus.py
```

The D-Bus service has full access as the installing user and includes process-control methods; install it only if you trust this code. The HTTP API is read-only and off by default in user mode; it binds to loopback unless you choose otherwise. Before exposing it to a network, set `NVSTATS_TOKEN`: samples include process names and command lines (secret-looking option values are masked, but that is a best-effort filter, not a guarantee).

## Other nvtop

I've leveraged this backend to display the same `nvtop` data in these projects:

- [Vobot nvtop](https://github.com/DAE51D/vobot-apps/tree/main/nvtop)
- [KDE knvtop](https://github.com/DAE51D/knvtop)
- [Openchamber onvtop](https://github.com/DAE51D/openchamber-onvtop)

So if you have this one service running those others will connect to it.
I encourage you to write other "clients" to this backend as well.