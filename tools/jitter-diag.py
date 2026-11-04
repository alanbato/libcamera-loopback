#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alan Velasco
"""jitter-diag.py — run with sudo. Stops libcamera-loopback, captures straight from
libcamera in a few variants and reports frame-arrival jitter: what makes a
stream stutter even though it averages 30 fps. Restarts libcamera-loopback afterwards.

Uses a source-built libcamera in /opt/libcamera if present (override with
LIBCAMERA_LIBDIR), otherwise the distribution's libcamera."""
import atexit, os, pwd, shutil, statistics, subprocess, sys, tempfile, time

if os.geteuid() != 0:
    sys.exit("run with sudo")

# A libcamera built from source (e.g. into /opt/libcamera) is used if present;
# otherwise the distribution's libcamera.
LC = os.environ.get("LIBCAMERA_LIBDIR", "/opt/libcamera/lib/x86_64-linux-gnu")
CUSTOM_LC = os.path.isdir(LC)
# The dynamic loader reads LD_LIBRARY_PATH only at process start, so re-exec
# ourselves with the environment set (otherwise GStreamer can't load the
# right libcamera plugin and may silently fall back to an older distro one).
# GStreamer's plugin registry goes in a fresh private (0700, root-owned)
# directory, never a predictable path in /tmp: this runs as root, and a
# planted file or symlink there could redirect writes or poison the cache.
if "JITTER_DIAG_TMPDIR" not in os.environ:
    tmp = tempfile.mkdtemp(prefix="jitter-diag-")
    env = dict(os.environ, GST_REGISTRY_1_0=os.path.join(tmp, "gst-registry.bin"),
               JITTER_DIAG_TMPDIR=tmp, LIBCAMERA_LOG_LEVELS="*:ERROR")
    if CUSTOM_LC:
        env.update(LD_LIBRARY_PATH=LC, GST_PLUGIN_PATH_1_0=f"{LC}/gstreamer-1.0")
    os.execve(sys.executable, [sys.executable, "-I", os.path.abspath(__file__)], env)


def _cleanup_tmp():
    tmp = os.environ.get("JITTER_DIAG_TMPDIR", "")
    if tmp and os.path.basename(tmp).startswith("jitter-diag-"):
        shutil.rmtree(tmp, ignore_errors=True)


atexit.register(_cleanup_tmp)
import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

VARIANTS = [
    ("A  current (GPU softISP, auto exposure)", {}, "libcamerasrc"),
    ("B  CPU softISP, auto exposure", {"LIBCAMERA_SOFTISP_MODE": "cpu"}, "libcamerasrc"),
    ("C  GPU softISP, AE OFF (fixed 30ms / gain 4)", {},
     "libcamerasrc ae-enable=false exposure-time=30000 analogue-gain=4.0"),
    ("D  GPU softISP, auto exposure + 5% write hysteresis (patched)",
     {"LIBCAMERA_SOFTISP_AGC_HYSTERESIS": "0.05"}, "libcamerasrc"),
    ("E  CPU softISP, auto exposure + 5% write hysteresis (patched)",
     {"LIBCAMERA_SOFTISP_MODE": "cpu", "LIBCAMERA_SOFTISP_AGC_HYSTERESIS": "0.05"}, "libcamerasrc"),
]
TAIL = ("! video/x-raw,width=1280,height=720,framerate=30/1 ! videoconvert ! "
        "video/x-raw,format=YUY2 ! appsink name=s emit-signals=true sync=false max-buffers=4 drop=false")


def csi_errors_since(t):
    out = subprocess.run(["journalctl", "-k", "--no-pager", "-S", t, "-o", "cat"],
                         capture_output=True, text=True).stdout
    n = sum(1 for l in out.splitlines() if "csi2-2 error" in l)
    for l in out.splitlines():
        if "callbacks suppressed" in l:
            n += int(l.split(":")[1].split()[0])
    return n


def run(label, env, src, warm=3.0, dur=12.0):
    for k in ("LIBCAMERA_SOFTISP_MODE", "LIBCAMERA_SOFTISP_AGC_HYSTERESIS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    Gst.init(None)
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        p = Gst.parse_launch(f"{src} {TAIL}")
    except Exception as e:
        print(f"{label}: pipeline error: {e}"); return
    arr = []

    def on_sample(sink):
        sink.emit("pull-sample")
        arr.append(time.monotonic())
        return Gst.FlowReturn.OK

    p.get_by_name("s").connect("new-sample", on_sample)
    p.set_state(Gst.State.PLAYING)
    t_end = time.monotonic() + warm + dur
    bus = p.get_bus()
    while time.monotonic() < t_end:
        m = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR)
        if m:
            print(f"{label}: ERROR {m.parse_error()[0].message}"); break
    p.set_state(Gst.State.NULL)
    t0 = arr[0] + warm if arr else 0
    a = [t for t in arr if t >= t0]
    if len(a) < 10:
        print(f"{label}: only {len(a)} frames"); return
    iv = [1000 * (y - x) for x, y in zip(a, a[1:])]
    print(f"{label}\n    {len(a)} frames {len(a)/(a[-1]-a[0]):.1f} fps | interval median {statistics.median(iv):.1f}"
          f" p95 {sorted(iv)[int(.95*len(iv))]:.1f} max {max(iv):.0f} ms | >50ms: {sum(x > 50 for x in iv)}"
          f" | <20ms: {sum(x < 20 for x in iv)} | CSI errors: {csi_errors_since(since)}")
    time.sleep(2)


Gst.init(None)
f = Gst.ElementFactory.find("libcamerasrc")
print(f"libcamerasrc from: {f.get_plugin().get_filename() if f else 'NOT FOUND'}"
      f" (version {f.get_plugin().get_version() if f else '-'})", flush=True)
if not f:
    sys.exit("libcamerasrc (libcamera's GStreamer plugin) not found; aborting")
if CUSTOM_LC and not f.get_plugin().get_filename().startswith(LC):
    sys.exit(f"expected the libcamerasrc plugin from {LC}; aborting")
subprocess.run(["systemctl", "stop", "libcamera-loopback"])
time.sleep(2)
try:
    for v in VARIANTS:
        run(*v)
finally:
    subprocess.run(["systemctl", "start", "libcamera-loopback"])
    time.sleep(2)
    # PipeWire only re-detects the loopback as a camera on re-probe
    user = os.environ.get("SUDO_USER")
    if user and user != "root":
        uid = pwd.getpwnam(user).pw_uid
        subprocess.run(["sudo", "-u", user, f"XDG_RUNTIME_DIR=/run/user/{uid}",
                        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                        "systemctl", "--user", "restart", "wireplumber"])
        print("libcamera-loopback + wireplumber restarted")
    else:
        print("libcamera-loopback restarted; run 'systemctl --user restart wireplumber' as your user")
