---
name: nvstats-install
description: Install, verify, update, or uninstall the shared nvstats GPU and model-process telemetry service on Linux. Use whenever someone asks an AI agent to set up nvstats-service or connect a local GPU monitoring client to it.
---

# nvstats-service install skill

Help the user install the shared telemetry daemon for the current desktop user. The service uses that user's session D-Bus; do not install it as root or assume a particular username, service account, host, or home directory.

## Install or update

1. Confirm this is a Linux host with a logged-in user session. If the user already has this repository, use that checkout; otherwise clone `https://github.com/DAE51D/nvstats-service.git`.
2. Check that Python 3 and its D-Bus/GLib bindings are available. On Debian/Ubuntu/Kubuntu, the packages are `python3-dbus` and `python3-gi`. Ask before using `sudo` to install missing packages; do not pipe remote scripts into a shell.
3. Run `./install.sh` from the repository as the desktop user, without `sudo`. It installs `nvstats.service` and the daemon under the invoking user's home and starts the user service. For a headless GPU host with no desktop session, use `sudo ./install.sh --system` instead (HTTP only; see the README for `--bind`, `--port`, `--token`). Only enable network exposure (`--bind` other than loopback) if the user asks for it, and suggest a `--token`. The installer also removes the legacy `vobot-gpu-daemon`; mention that before running it if you see one.
4. Verify `systemctl --user is-active nvstats.service` reports `active`. Then call `busctl --user call com.daevid.nvstats /com/daevid/nvstats com.daevid.nvstats.Sample Get`. For HTTP installs also check `curl -s http://HOST:PORT/health`. Do not paste the full JSON sample into chat or logs: process arguments can contain private values. Report only a small, sanitized summary such as whether GPU/system data is present and how many model processes were detected.

## Process visibility

The daemon can read processes owned by the same user without extra privileges. Basic process names and arguments may also be visible across users depending on the host's `/proc` policy. Per-process GPU engine/memory details for another user's process require a dedicated interpreter with `CAP_SYS_PTRACE`; this is optional and is a broad process-inspection privilege.

Do not grant that capability by default. Explain the tradeoff and ask for explicit approval first. If approved, follow the generic cross-user procedure in the knvtop README and apply the capability only to the dedicated `python3-ptrace` copy—not the system Python executable. Never change sudoers or run the telemetry daemon as root to avoid this optional step.

## Uninstall

Only when the user explicitly asks to uninstall, stop and disable `nvstats.service`, remove that user's `~/.config/systemd/user/nvstats.service` and `~/.local/share/nvstats-service`, then reload the user systemd manager. Ask before removing the optional system-wide capability helper; it may be shared by other clients.

## Client setup

- For the Plasma widget, follow the [knvtop install skill](https://github.com/DAE51D/knvtop/blob/main/SKILL.md) and add the widget through Plasma's UI.
- For OpenChamber, follow the [onvtop install skill](https://github.com/DAE51D/openchamber-onvtop/blob/main/SKILL.md). The OpenChamber host and nvstats daemon must run as the same user.
- A single nvstats installation can serve both clients. Do not install a second poller or invent a legacy D-Bus alias.
