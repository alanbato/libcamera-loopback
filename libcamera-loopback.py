#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alan Velasco
"""libcamera-loopback — on-demand camera -> v4l2loopback relay that writes frames with write().

A small replacement for v4l2-relayd. v4l2-relayd has to feed v4l2loopback through
GStreamer's v4l2sink, which only supports mmap output and in practice cycles
just 1-2 of the loopback's buffers; v4l2loopback then hands readers buffers they
are still holding, so GStreamer's v4l2src fails ("buffer N was not queued") and
PipeWire silently drops every frame. Writing with write() makes v4l2loopback
rotate through all of its buffers itself, and every reader works.

Behaviour (same contract as v4l2-relayd):
  * idle: hold the loopback device open as the writer, set the format and write
    one black frame so the device advertises CAPTURE (exclusive_caps=1). The
    camera itself stays off.
  * a reader starts streaming -> v4l2loopback raises its CLIENT_USAGE event
    (count=1) -> start the GStreamer source (libcamerasrc) and write() frames.
  * the last reader stops -> count=0 -> after --linger seconds, stop the source
    (camera and privacy LED off).
"""
import argparse
import fcntl
import os
import signal
import struct
import time
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

__version__ = "0.1.0"

# --- V4L2 ABI (x86_64) -------------------------------------------------------
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
VIDIOC_S_FMT = 0xC0D05605            # _IOWR('V', 5, struct v4l2_format[208])
VIDIOC_S_PARM = 0xC0CC5616           # _IOWR('V', 22, struct v4l2_streamparm[204])
VIDIOC_SUBSCRIBE_EVENT = 0x4020565A  # _IOW('V', 90, struct v4l2_event_subscription[32])
VIDIOC_DQEVENT = 0x80885659          # _IOR('V', 89, struct v4l2_event[136])
V4L2_EVENT_SUB_FL_SEND_INITIAL = 1
# v4l2loopback: V4L2_EVENT_PRIVATE_START + 0x08E00000 + 1
V4L2_EVENT_PRI_CLIENT_USAGE = 0x08000000 + 0x08E00000 + 1


def fourcc(s):
    return s[0] | (s[1] << 8) | (s[2] << 16) | (s[3] << 24)


def log(*a):
    print("libcamera-loopback:", *a, file=sys.stderr, flush=True)


class Relay:
    def __init__(self, args):
        self.a = args
        self.w, self.h, self.fps = args.width, args.height, args.fps
        self.pixfmt = args.format
        if self.pixfmt == "YUY2":
            self.frame_size, self.bpl = self.w * self.h * 2, self.w * 2
        else:  # NV12
            self.frame_size, self.bpl = self.w * self.h * 3 // 2, self.w
        self.pipeline = None
        self.frames = 0
        self.stop_timer = 0  # pending delayed stop (GLib source id)
        self.st = None       # frame timing stats window
        self.fd = os.open(args.device, os.O_RDWR)
        self._set_format()
        self._write_black()          # makes the device advertise CAPTURE
        self._subscribe()
        GLib.io_add_watch(self.fd, GLib.PRIORITY_DEFAULT, GLib.IO_PRI, self._on_event)

    # -- device setup --
    def _set_format(self):
        v4l2_fourcc = fourcc(b"YUYV") if self.pixfmt == "YUY2" else fourcc(b"NV12")
        pix = struct.pack("12I", self.w, self.h, v4l2_fourcc, V4L2_FIELD_NONE,
                          self.bpl, self.frame_size, 0, 0, 0, 0, 0, 0)
        fmt = struct.pack("I4x", V4L2_BUF_TYPE_VIDEO_OUTPUT) + pix.ljust(200, b"\0")
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, bytearray(fmt))
        # output timeperframe = 1/fps (what readers see as the frame rate)
        parm = struct.pack("7I", V4L2_BUF_TYPE_VIDEO_OUTPUT, 0, 0, 1, self.fps, 0, 0)
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_PARM, bytearray(parm.ljust(204, b"\0")))
        except OSError as e:
            log("S_PARM failed (non-fatal):", e)
        log(f"format {self.pixfmt} {self.w}x{self.h}@{self.fps} on {self.a.device}"
            f" (linger {self.a.linger}s)")

    def _write_black(self):
        if self.pixfmt == "YUY2":
            os.write(self.fd, b"\x10\x80" * (self.frame_size // 2))
        else:
            y = self.w * self.h
            os.write(self.fd, b"\x10" * y + b"\x80" * (self.frame_size - y))

    def _subscribe(self):
        sub = struct.pack("8I", V4L2_EVENT_PRI_CLIENT_USAGE, 0,
                          V4L2_EVENT_SUB_FL_SEND_INITIAL, 0, 0, 0, 0, 0)
        fcntl.ioctl(self.fd, VIDIOC_SUBSCRIBE_EVENT, bytearray(sub))

    # -- reader tracking --
    def _on_event(self, fd, cond):
        while True:
            ev = bytearray(136)
            try:
                fcntl.ioctl(self.fd, VIDIOC_DQEVENT, ev)
            except OSError:
                break  # no more pending events
            etype, = struct.unpack_from("I", ev, 0)
            count, = struct.unpack_from("I", ev, 8)
            pending, = struct.unpack_from("I", ev, 72)
            if etype == V4L2_EVENT_PRI_CLIENT_USAGE:
                log(f"client usage: {count}")
                if count:
                    self._cancel_delayed_stop()
                    self.start()
                elif self.pipeline and not self.stop_timer:
                    # apps often stop+restart streaming while (re)negotiating at
                    # startup; keep the camera running briefly so they don't race
                    # a full libcamera teardown/restart.
                    self.stop_timer = GLib.timeout_add(int(self.a.linger * 1000),
                                                       self._delayed_stop)
            if not pending:
                break
        return True

    def _cancel_delayed_stop(self):
        if self.stop_timer:
            GLib.source_remove(self.stop_timer)
            self.stop_timer = 0

    def _delayed_stop(self):
        self.stop_timer = 0
        self.stop()
        return False

    # -- capture pipeline --
    def start(self):
        if self.pipeline:
            return
        desc = (f"{self.a.source} ! video/x-raw,width={self.w},height={self.h},"
                f"framerate={self.fps}/1 ! videoconvert ! videoscale ! "
                f"video/x-raw,format={self.pixfmt},width={self.w},height={self.h} ! "
                "queue leaky=downstream max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! "
                "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false")
        log("starting:", desc)
        self.pipeline = Gst.parse_launch(desc)
        self.pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self.frames = 0
        self.st = None
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self._cancel_delayed_stop()
        if not self.pipeline:
            return
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_signal_watch()
        self.pipeline = None
        log(f"stopped after {self.frames} frames")
        self._write_black()  # late readers see black, not a stale frame

    def _on_sample(self, sink):
        t0 = time.monotonic()
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = info.data
                if len(data) >= self.frame_size:
                    os.write(self.fd, data[: self.frame_size])
                    self.frames += 1
            except OSError as e:
                log("write failed:", e)
            finally:
                buf.unmap(info)
        if self.a.stats:
            self._stats(t0, time.monotonic())
        return Gst.FlowReturn.OK

    # -- frame timing stats (--stats N: log every N seconds while streaming) --
    def _stats(self, t0, t1):
        """Log frame rate, late arrivals (> 1.5 frame intervals) and the slowest
        write callback. Late arrivals with a steady average fps = stutter."""
        s = self.st
        if s is None or t1 - s["t_log"] > 30:  # new stream, or a long pause
            s = self.st = {"t_log": t1, "n": 0, "last_t": None,
                           "late": 0, "max_gap": 0.0, "cb_max": 0.0}
        frame = 1.0 / self.fps
        if s["last_t"] is not None:
            d = t0 - s["last_t"]
            s["max_gap"] = max(s["max_gap"], d)
            if d > 1.5 * frame:
                s["late"] += 1
        s["cb_max"] = max(s["cb_max"], t1 - t0)
        s["last_t"] = t0
        s["n"] += 1
        if t1 - s["t_log"] >= self.a.stats:
            el = t1 - s["t_log"]
            log(f"stats {el:.0f}s: {s['n']} frames = {s['n']/el:.1f} fps | arrivals >"
                f"{1500*frame:.0f}ms: {s['late']} (max {1000*s['max_gap']:.0f} ms) | "
                f"write callback max {1000*s['cb_max']:.1f} ms")
            self.st = {**s, "t_log": t1, "n": 0, "late": 0, "max_gap": 0.0, "cb_max": 0.0}

    def _on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log("pipeline error:", err.message, "|", dbg)
        GLib.idle_add(self.stop)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"libcamera-loopback {__version__}")
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--format", choices=["NV12", "YUY2"], default="YUY2",
                   help="pixel format offered to apps (YUY2 = what real webcams use)")
    p.add_argument("--linger", type=float, default=3.0,
                   help="seconds to keep the camera running after the last reader stops")
    p.add_argument("--source", default="libcamerasrc",
                   help='GStreamer source, e.g. "videotestsrc is-live=true" for testing')
    p.add_argument("--stats", type=float, default=0,
                   help="log frame-timing stats every N seconds while streaming (0 = off)")
    p.add_argument("--label", help='find the loopback device by card label, e.g. "Intel MIPI Camera"')
    args = p.parse_args()
    if not args.source:  # e.g. an empty LOOPBACK_SOURCE in /etc/default/libcamera-loopback
        args.source = "libcamerasrc"
    if args.label:
        base = "/sys/devices/virtual/video4linux"
        for d in sorted(os.listdir(base)):
            try:
                with open(f"{base}/{d}/name") as f:
                    if f.read().strip() == args.label:
                        args.device = f"/dev/{d}"
                        break
            except OSError:
                pass
        else:
            sys.exit(f"libcamera-loopback: no v4l2loopback device labelled {args.label!r}")
    Gst.init(None)
    relay = Relay(args)
    loop = GLib.MainLoop()

    def on_signal(*_):
        relay.stop()
        loop.quit()
        return GLib.SOURCE_REMOVE

    for s in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, s, on_signal)
    log("ready; waiting for readers")
    loop.run()


if __name__ == "__main__":
    main()
