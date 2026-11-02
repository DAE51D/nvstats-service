# nvstats-service

The standalone, per-user D-Bus telemetry daemon shared by GPU monitoring clients. It polls GPU/system/process data once and publishes the latest sample on the current user's session bus. The Plasma knvtop widget and OpenChamber onvtop extension are independent clients; neither contains its own sampler.

The D-Bus API uses the neutral `com.daevid.nvstats` bus name, `/com/daevid/nvstats` object path, and `com.daevid.nvstats.*` interfaces:

- `com.daevid.nvstats.Sample.Get()` returns the latest `{gpu, sys, llm, gpu_procs, current_user, can_kill_all}` sample.
- `com.daevid.nvstats.Status.IsAlive()` and `Restart()` manage the poller.
- `com.daevid.nvstats.Process.Kill()` and `Restart()` provide process controls.

The clients in this release use this API directly; there is no legacy bus-name alias.

## Install

This is a user-session service; do not run the installer with `sudo`. It requires Python 3 with the distribution's D-Bus and GLib bindings. On Debian/Ubuntu/Kubuntu:

```sh
sudo apt install python3-dbus python3-gi
git clone https://github.com/DAE51D/nvstats-service.git
cd nvstats-service
./install.sh
```

The installer copies the daemon to `~/.local/share/nvstats-service`, installs and enables the `nvstats.service` user unit, and starts it. GPU tools are optional: `amd-smi`/`nvidia-smi` are used when available, with sysfs fallbacks retained.

Verify it is running:

```sh
systemctl --user status nvstats.service
busctl --user call com.daevid.nvstats /com/daevid/nvstats com.daevid.nvstats.Sample Get
```

Once installed, both knvtop and onvtop can consume the daemon independently; neither the widget nor OpenChamber extension needs to be installed to keep telemetry polling alive.

## Optional cross-user GPU process data

The daemon can read per-process GPU engine/memory details only for processes owned by the daemon user unless its interpreter has `CAP_SYS_PTRACE`. This is optional; GPU-wide and system-wide telemetry still works without it. To grant the capability only to a dedicated Python copy, follow the cross-user GPU stats instructions in the [knvtop documentation](https://github.com/DAE51D/knvtop#cross-user-gpu-stats-per-process-gpuvram-showing-na). Do not set the capability on the system-wide Python interpreter.

## Development checks

```sh
python3 -m py_compile service/nvstats_service.py backend/nvstats_poll.py
bash -n install.sh
dbus-run-session -- python3 scripts/smoke_dbus.py
```

The D-Bus service has full access as the installing user and includes process-control methods; install it only if you trust this code. It does not listen on a network port.
