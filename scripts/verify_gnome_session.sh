#!/usr/bin/env bash
# Disposable compositor fixture; run inside dbus-run-session, never a personal session.
set -euo pipefail
qcu_python=${QCU_PYTHON:-.venv/bin/python}
qcu_evidence=${QCU_EVIDENCE_DIR:-wayland-evidence}
mkdir -p "$qcu_evidence"
export XDG_RUNTIME_DIR=$(mktemp -d) XDG_DATA_HOME=$(mktemp -d) XDG_CONFIG_HOME=$(mktemp -d)
export XDG_CACHE_HOME=$(mktemp -d) XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME
export GSETTINGS_BACKEND=keyfile LIBGL_ALWAYS_SOFTWARE=1 NO_AT_BRIDGE=0 GTK_MODULES=gail:atk-bridge
unset DISPLAY WAYLAND_DISPLAY
"$qcu_python" -m qcu.gnome_bridge install
mkdir -p "$XDG_DATA_HOME/gnome-shell/extensions/qcu-test@quick-computer-use"
cp tests/fixtures/gnome-control/* "$XDG_DATA_HOME/gnome-shell/extensions/qcu-test@quick-computer-use/"
gsettings set org.gnome.shell enabled-extensions '["qcu@quick-computer-use","qcu-test@quick-computer-use"]'
gsettings set org.gnome.desktop.interface enable-animations false
qcu_shell_version=$(gnome-shell --version | awk '{print $3}')
gsettings set org.gnome.shell welcome-dialog-last-shown-version "$qcu_shell_version"
gnome-shell --version >"$qcu_evidence/version.txt"
gnome-shell --headless --wayland --virtual-monitor 1280x1024 --no-x11 >"$qcu_evidence/gnome.log" 2>&1 &
qcu_shell_pid=$!
qcu_cleanup() {
    kill "$qcu_shell_pid" 2>/dev/null || true
    wait "$qcu_shell_pid" 2>/dev/null || true
}
trap qcu_cleanup EXIT
for n in $(seq 1 100); do
    if test -S "$XDG_RUNTIME_DIR/wayland-0"; then break; fi
    kill -0 "$qcu_shell_pid"
    sleep .25
done
export WAYLAND_DISPLAY=wayland-0 GDK_BACKEND=wayland
"$qcu_python" scripts/verify_linux_atspi.py --output "$qcu_evidence/linux-live.json"
