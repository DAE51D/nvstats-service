#!/usr/bin/env bash
# Install nvstats-service.
#
#   ./install.sh                      user mode: D-Bus service for knvtop/onvtop
#   ./install.sh --http               ... plus the HTTP API (loopback only)
#   ./install.sh --http --bind 0.0.0.0 [--port 8039] [--token SECRET]
#   sudo ./install.sh --system        headless host: HTTP-only system service
#                                     (no D-Bus, no desktop session needed)
#
# Either mode removes the legacy vobot-gpu-daemon if it is installed
# (--keep-legacy to leave it, e.g. while it still owns port 8039).
# Settings live in an env file that is created once and never overwritten
# unless you pass the matching option:
#   user:   ~/.config/nvstats/nvstats.env
#   system: /etc/nvstats/nvstats.env
set -euo pipefail

mode=user
http=0
bind=""
port=""
token=""
keep_legacy=0

while [[ $# -gt 0 ]]; do
	case "$1" in
	--system) mode=system ;;
	--http) http=1 ;;
	--bind) bind="${2:?--bind needs an address}"; http=1; shift ;;
	--port) port="${2:?--port needs a number}"; http=1; shift ;;
	--token) token="${2:?--token needs a value}"; http=1; shift ;;
	--keep-legacy) keep_legacy=1 ;;
	-h | --help) sed -n '2,16p' "$0"; exit 0 ;;
	*) echo "Unknown option: $1" >&2; exit 2 ;;
	esac
	shift
done

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$(command -v python3 || true)"
[[ -n "$python" ]] || { echo "python3 is required." >&2; exit 1; }

if [[ "$mode" == system ]]; then
	[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "--system needs root: sudo ./install.sh --system" >&2; exit 1; }
	install_dir=/opt/nvstats-service
	env_file=/etc/nvstats/nvstats.env
	[[ -n "$bind" ]] || bind=0.0.0.0
else
	if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
		echo "Run user mode as your desktop user, not root (use --system for a headless host)." >&2
		exit 1
	fi
	if ! "$python" -c 'import dbus; from gi.repository import GLib' >/dev/null 2>&1; then
		echo "Python D-Bus/GLib bindings are missing. On Debian/Ubuntu:" >&2
		echo "  sudo apt install python3-dbus python3-gi" >&2
		echo "(or use sudo ./install.sh --system for an HTTP-only install)" >&2
		exit 1
	fi
	install_dir="$HOME/.local/share/nvstats-service"
	env_file="${XDG_CONFIG_HOME:-$HOME/.config}/nvstats/nvstats.env"
	unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
fi

as_root() { if [[ ${EUID:-$(id -u)} -eq 0 ]]; then "$@"; else sudo "$@"; fi; }

# --- legacy vobot-gpu-daemon -------------------------------------------------
if [[ $keep_legacy -eq 0 ]] && systemctl list-unit-files vobot-gpu-daemon.service 2>/dev/null | grep -q vobot-gpu-daemon; then
	echo "Removing legacy vobot-gpu-daemon (superseded by nvstats HTTP)..."
	as_root systemctl disable --now vobot-gpu-daemon.service || true
	as_root rm -f /etc/systemd/system/vobot-gpu-daemon.service
	as_root rm -rf /opt/vobot-gpu-daemon
	as_root systemctl daemon-reload
fi

# --- files -------------------------------------------------------------------
commit="$(git -C "$repo_dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [[ "$commit" != unknown ]] && ! git -C "$repo_dir" diff --quiet HEAD -- . 2>/dev/null; then
	commit="$commit-dirty"
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/service" "$stage/backend"
cp "$repo_dir"/service/nvstats_service.py "$repo_dir"/service/nvstats_httpd.py "$stage/service/"
cp "$repo_dir"/backend/nvstats_core.py "$repo_dir"/backend/nvstats_gpu.py "$repo_dir"/backend/nvstats_poll.py "$stage/backend/"
sed -i "s/^GIT_COMMIT = .*/GIT_COMMIT = \"$commit\"/" "$stage/backend/nvstats_core.py"

if [[ "$mode" == system ]]; then
	as_root install -d "$install_dir/service" "$install_dir/backend" "$(dirname "$env_file")"
	as_root install -m 0644 "$stage"/service/*.py "$install_dir/service/"
	as_root install -m 0644 "$stage"/backend/*.py "$install_dir/backend/"
	as_root install -m 0644 "$repo_dir/nvstats-httpd.service" /etc/systemd/system/nvstats-httpd.service
else
	install -d "$install_dir/service" "$install_dir/backend" "$unit_dir" "$(dirname "$env_file")"
	install -m 0644 "$stage"/service/*.py "$install_dir/service/"
	install -m 0644 "$stage"/backend/*.py "$install_dir/backend/"
	install -m 0644 "$repo_dir/nvstats.service" "$unit_dir/nvstats.service"
fi

# --- env file: create once, update only keys given on the command line --------
write_env() { if [[ "$mode" == system ]]; then as_root tee "$env_file" >/dev/null; else cat >"$env_file"; fi; }
read_env() { if [[ -r "$env_file" ]]; then cat "$env_file"; fi; }
set_key() { # set_key KEY VALUE
	local body
	body="$(read_env | grep -v "^$1=" || true)"
	printf '%s\n%s=%s\n' "$body" "$1" "$2" | sed '/^$/d' | write_env
}
[[ -e "$env_file" ]] || { printf '# nvstats settings (see README)\n' | write_env; }
if [[ $http -eq 1 || "$mode" == system ]]; then
	[[ "$mode" == user ]] && set_key NVSTATS_HTTP 1
	[[ -n "$bind" ]] && set_key NVSTATS_HTTP_BIND "$bind"
	[[ -n "$port" ]] && set_key NVSTATS_HTTP_PORT "$port"
	[[ -n "$token" ]] && set_key NVSTATS_TOKEN "$token"
fi
if [[ "$mode" == system ]]; then
	as_root chmod 0640 "$env_file"
	as_root chgrp "$(id -gn nobody 2>/dev/null || echo root)" "$env_file" 2>/dev/null || true
else
	chmod 0600 "$env_file"
fi

# --- (re)start ---------------------------------------------------------------
if [[ "$mode" == system ]]; then
	systemctl daemon-reload
	systemctl enable nvstats-httpd.service
	systemctl restart nvstats-httpd.service
	echo "nvstats-httpd installed (system). Check: systemctl status nvstats-httpd.service"
	grep -q '^NVSTATS_TOKEN=' "$env_file" || echo "Note: listening on $bind without NVSTATS_TOKEN; add one with --token if this network is not trusted."
else
	systemctl --user daemon-reload
	systemctl --user enable nvstats.service
	if systemctl --user is-active --quiet nvstats.service; then
		systemctl --user restart nvstats.service
	else
		systemctl --user start nvstats.service
	fi
	echo "nvstats-service installed. Check it with: systemctl --user status nvstats.service"
fi
