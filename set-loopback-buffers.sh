#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alan Velasco
#
# set-loopback-buffers.sh [N] — set v4l2loopback's max_buffers (default 3),
# persist it in /etc/modprobe.d/v4l2loopback-maxbuffers.conf and reload the
# module now (run with sudo). PipeWire and the relay keep the device open, so
# they are stopped for a few seconds (audio blips) and always restarted.
#
# Why 3: v4l2loopback grants every reader the *writer's* buffer count, no matter
# how many the reader asked for, then delivers frames in all of them. Apps that
# only set up the count they requested (and don't check the count granted) end
# up reading through buffer indices they never mapped and crash or show garbage.
# Granting fewer buffers than requested is normal V4L2 behaviour that apps
# handle, so keep this at the smallest count apps commonly request.
set -u
N=${1:-3}
CONF=/etc/modprobe.d/v4l2loopback-maxbuffers.conf
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

U=${SUDO_USER:-}
as_user() {
    [ -n "$U" ] && [ "$U" != root ] || return 0
    local uid; uid=$(id -u "$U")
    sudo -u "$U" XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}
PW_UNITS=(pipewire.socket pipewire-pulse.socket pipewire.service pipewire-pulse.service wireplumber.service)
RELAYS=()
for r in libcamera-loopback.service v4l2-relayd@default.service; do
    if systemctl is-enabled --quiet "$r" 2>/dev/null; then
        RELAYS+=("$r")
    fi
done

restart_all() {
    # the relay first, so PipeWire probes the device while it's a capture device
    if [ ${#RELAYS[@]} -gt 0 ]; then
        systemctl start "${RELAYS[@]}"
    fi
    sleep 2
    as_user systemctl --user start "${PW_UNITS[@]}"
    for r in "${RELAYS[@]}"; do echo ">>> $r: $(systemctl is-active "$r")"; done
}
trap restart_all EXIT

echo ">>> Writing $CONF (max_buffers=$N)"
printf '%s\n' "# Written by libcamera-loopback's set-loopback-buffers.sh; see that script for why." \
    "options v4l2loopback max_buffers=$N" > "$CONF"

echo ">>> Stopping PipeWire and relay(s): ${RELAYS[*]:-none}"
as_user systemctl --user stop "${PW_UNITS[@]}"
if [ ${#RELAYS[@]} -gt 0 ]; then
    systemctl stop "${RELAYS[@]}"
fi
sleep 1

echo ">>> Reloading v4l2loopback"
if modprobe -r v4l2loopback; then
    modprobe v4l2loopback
    echo "    max_buffers=$(cat /sys/module/v4l2loopback/parameters/max_buffers)"
else
    echo "    reload failed (device still in use); the setting applies after a reboot:"
    fuser -v /dev/video* 2>&1 | head
fi
