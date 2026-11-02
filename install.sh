#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
	echo "Run install.sh as your desktop user, not with sudo." >&2
	exit 1
fi

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install_dir="$HOME/.local/share/knvtop-service"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
python="$(command -v python3 || true)"

if [[ -z "$python" ]]; then
	echo "python3 is required." >&2
	exit 1
fi

if ! "$python" -c 'import dbus; from gi.repository import GLib' >/dev/null 2>&1; then
	echo "Python D-Bus/GLib bindings are missing. On Debian/Ubuntu install them with:" >&2
	echo "  sudo apt install python3-dbus python3-gi" >&2
	exit 1
fi

install -d "$install_dir/service" "$install_dir/backend" "$unit_dir"
install -m 0644 "$repo_dir/service/knvtop_service.py" "$install_dir/service/knvtop_service.py"
install -m 0644 "$repo_dir/backend/knvtop_poll.py" "$install_dir/backend/knvtop_poll.py"
install -m 0644 "$repo_dir/knvtop.service" "$unit_dir/knvtop.service"

systemctl --user daemon-reload
systemctl --user enable knvtop.service
if systemctl --user is-active --quiet knvtop.service; then
	systemctl --user restart knvtop.service
else
	systemctl --user start knvtop.service
fi

echo "knvtop-service installed. Check it with: systemctl --user status knvtop.service"
