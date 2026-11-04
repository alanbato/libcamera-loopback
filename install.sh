#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alan Velasco
#
# install.sh — install/update libcamera-loopback as a systemd service (run with sudo).
#   sudo ./install.sh              install or update (keeps /etc/default/libcamera-loopback)
#   sudo ./install.sh --uninstall  remove libcamera-loopback and re-enable v4l2-relayd if present
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
LIBDIR=/usr/local/lib/libcamera-loopback
UNIT=/etc/systemd/system/libcamera-loopback.service
DEFAULTS=/etc/default/libcamera-loopback
RELAYD=v4l2-relayd@default.service

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# Restart WirePlumber for the desktop user who ran sudo: PipeWire only notices
# that the loopback device became a camera when it re-probes it.
restart_wireplumber() {
    local u=${SUDO_USER:-}
    if [ -z "$u" ] || [ "$u" = root ]; then
        echo ">>> (restart WirePlumber yourself: systemctl --user restart wireplumber)"
        return 0
    fi
    local uid; uid=$(id -u "$u")
    [ -S "/run/user/$uid/bus" ] || return 0
    echo ">>> Restarting WirePlumber for $u so PipeWire re-detects the camera"
    sudo -u "$u" XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        systemctl --user restart wireplumber || true
}

relayd_installed() { [ -e /usr/lib/systemd/system/v4l2-relayd@.service ]; }

if [ "${1:-}" = "--uninstall" ]; then
    echo ">>> Removing libcamera-loopback"
    systemctl disable --now libcamera-loopback.service 2>/dev/null || true
    rm -f "$UNIT"
    rm -rf "$LIBDIR"
    echo "    (kept $DEFAULTS; delete it by hand if you don't need it)"
    systemctl daemon-reload
    if relayd_installed; then
        echo ">>> Re-enabling v4l2-relayd"
        systemctl unmask "$RELAYD" 2>/dev/null || true
        systemctl start v4l2-relayd "$RELAYD" || true
    fi
    restart_wireplumber
    exit 0
fi

echo ">>> Installing libcamera-loopback.py to $LIBDIR (root-owned)"
install -d -m 755 "$LIBDIR"
install -m 755 "$HERE/libcamera-loopback.py" "$LIBDIR/libcamera-loopback.py"

echo ">>> Installing $UNIT"
install -m 644 "$HERE/libcamera-loopback.service" "$UNIT"

if [ -e "$DEFAULTS" ]; then
    echo ">>> Keeping existing $DEFAULTS"
else
    echo ">>> Installing default settings to $DEFAULTS"
    install -m 644 "$HERE/libcamera-loopback.default" "$DEFAULTS"
fi

if relayd_installed; then
    echo ">>> Stopping and masking v4l2-relayd (undo with --uninstall)"
    systemctl stop "$RELAYD" v4l2-relayd 2>/dev/null || true
    systemctl mask "$RELAYD"
fi

echo ">>> Enabling libcamera-loopback"
systemctl daemon-reload
systemctl enable libcamera-loopback.service
systemctl restart libcamera-loopback.service
sleep 3
restart_wireplumber
systemctl --no-pager status libcamera-loopback.service | head -10 || true
