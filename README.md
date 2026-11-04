# libcamera-loopback

An on-demand relay from a [libcamera](https://libcamera.org) camera to a
[v4l2loopback](https://github.com/v4l2loopback/v4l2loopback) device, so that
ordinary apps (browsers, video-call apps, PipeWire) can use **MIPI laptop
cameras** — like those on Intel IPU6 laptops — as if they were regular webcams.

It's a small drop-in replacement for `v4l2-relayd`, written after the stock
Ubuntu IPU6 stack stopped working on a Dell XPS 13 Plus (9320). The main
difference is that libcamera-loopback writes frames into the loopback device with
`write()` instead of GStreamer's `v4l2sink`. That turns out to matter a lot:
see [Why not v4l2-relayd?](#why-not-v4l2-relayd)

```
sensor → IPU6 (kernel) → libcamera + software ISP → libcamera-loopback → v4l2loopback /dev/videoN → apps
```

- Starts the camera only while an app is reading, and turns it off (LED off) a
  few seconds after the last app stops.
- One Python file, standard library + GStreamer bindings; no build step.
- Includes an optional libcamera patch that fixes stutter on sensors behind
  slow I²C buses, plus two small diagnostic tools.

**Status:** works on the author's XPS 13 Plus 9320 (OmniVision OV01A10, IPU6,
Ubuntu 24.04, kernel 7.0) with Chrome and Firefox-based browsers. It's a
personal project shared in case it helps; expect to adapt it to your machine.

*libcamera-loopback is an independent project. It is not part of, or endorsed
by, the libcamera or v4l2loopback projects; the name just says what it connects.*

## Requirements

- **v4l2loopback** with `exclusive_caps=1` and a card label, e.g. in
  `/etc/modprobe.d/v4l2loopback.conf`:
  ```
  options v4l2loopback devices=1 exclusive_caps=1 card_label="Intel MIPI Camera"
  ```
  (Ubuntu's IPU6 packages already set this up.) See also
  [buffer count](#v4l2loopback-buffer-count) below.
- **libcamera with the software ISP and its GStreamer plugin (`libcamerasrc`)**.
  For IPU6 that means a recent libcamera (this was developed against **v0.8.0**);
  Ubuntu 24.04's libcamera 0.2 is too old. See [Building libcamera](#building-libcamera).
- Python 3 with PyGObject and GStreamer 1.x (`python3-gi`,
  `gir1.2-gstreamer-1.0`, `gstreamer1.0-plugins-base`).
- systemd.

## Install

```bash
git clone https://github.com/alanbato/libcamera-loopback.git && cd libcamera-loopback
sudo ./install.sh
```

`install.sh` copies `libcamera-loopback.py` to `/usr/local/lib/libcamera-loopback/` (root-owned:
the service never runs code from your home directory), installs
`libcamera-loopback.service`, writes default settings to `/etc/default/libcamera-loopback` (only if
that file doesn't exist yet), stops and masks `v4l2-relayd@default` if it is
installed, starts libcamera-loopback and restarts WirePlumber for your user.

Re-run it after changing `libcamera-loopback.py`. Undo everything with:

```bash
sudo ./install.sh --uninstall     # also unmasks and restarts v4l2-relayd if present
```

## Configuration

Edit `/etc/default/libcamera-loopback`, then `sudo systemctl restart libcamera-loopback` and
`systemctl --user restart wireplumber`.

| Setting | Default | Meaning |
|---|---|---|
| `LOOPBACK_LABEL` | `Intel MIPI Camera` | Card label of the loopback device to feed |
| `--width/--height/--fps` (in `LOOPBACK_ARGS`) | `1280 720 30` | Output size and frame rate |
| `--format` (in `LOOPBACK_ARGS`) | `YUY2` | `YUY2` (what real webcams use; most compatible) or `NV12` |
| `--linger` (in `LOOPBACK_ARGS`) | `3` | Seconds to keep the camera running after the last reader stops. Many apps stop and restart their stream while starting up; this avoids restarting the camera in the middle of that. |
| `LOOPBACK_SOURCE` | `libcamerasrc` | GStreamer source (may include properties, e.g. `libcamerasrc saturation=1.2`); `videotestsrc is-live=true` is handy for testing |
| `--stats` (in `LOOPBACK_ARGS`) | off | Log frame rate and late frames every N seconds |
| `LD_LIBRARY_PATH`, `GST_PLUGIN_PATH_1_0` | `/opt/libcamera/...` | Where your libcamera build lives |
| `LIBCAMERA_SOFTISP_AGC_HYSTERESIS` | `0.05` | Only used with the patch below |

Check it's working: `journalctl -u libcamera-loopback -f` should show `client usage: 1`
and `starting: libcamerasrc ...` when an app opens the camera.

## Building libcamera

Roughly (adjust paths to taste; this keeps your distro's libcamera untouched):

```bash
git clone --branch v0.8.0 https://git.libcamera.org/libcamera/libcamera.git
cd libcamera
git verify-tag v0.8.0            # optional: release tags are GPG-signed
git apply /path/to/libcamera-loopback/patches/libcamera-softisp-agc-hysteresis.patch   # optional, see below
meson setup build --prefix=/opt/libcamera --buildtype=release --wrap-mode=nodownload \
  -Dpipelines=simple -Dipas=softisp -Dgstreamer=enabled -Dcam=enabled \
  -Dqcam=disabled -Dpycamera=disabled -Ddocumentation=disabled -Dtest=false
ninja -C build
sudo ninja -C build install
```

On Ubuntu 24.04 you'll need roughly: `meson ninja-build pkg-config libyaml-dev
libudev-dev libevent-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
libdrm-dev python3-yaml python3-ply python3-jinja2 libgnutls28-dev openssl`.
Run meson with the system Python (`/usr/bin/python3`) if you use pyenv/uv/mise
shims.

### The AGC patch (recommended on IPU6 laptops)

`patches/libcamera-softisp-agc-hysteresis.patch` fixes **stutter**: the stream
averages 30 fps but frames arrive in bunches. The software ISP's auto exposure
sends new exposure/gain values to the sensor on every frame, and libcamera
writes them synchronously at the start of each frame. On many IPU6 laptops the
sensor's I²C goes through a slow USB bridge (Intel LJCA) at 5–10 ms per write,
so frame delivery stalls. With the patch and
`LIBCAMERA_SOFTISP_AGC_HYSTERESIS=0.05`, only changes larger than 5% are sent.
Measured on an XPS 13 Plus: late frames (>50 ms) went from 63–85 per 12 s to
**0**, with auto exposure still working. Without the variable the patch does
nothing.

## Why not v4l2-relayd?

`v4l2-relayd` can only feed the loopback through GStreamer's `v4l2sink`, which
uses shared (mmap) buffers. Its `io-mode=rw` isn't implemented for output. In
practice it wrote every frame into the **same buffer index**. v4l2loopback
hands buffers to readers in the order the writer used them, so readers kept
receiving a buffer they were still holding:

- GStreamer's `v4l2src`: `buffer 0 was not queued, this indicate a driver bug`
- PipeWire: dequeues every frame and silently forwards none (apps hang)

When the writer uses `write()`, v4l2loopback rotates through its buffers
itself and all of these readers work. libcamera-loopback keeps the rest of the
v4l2-relayd contract: it subscribes to v4l2loopback's client-usage event to
know when apps start and stop reading, and keeps the device advertising
`CAPTURE` while idle by writing one black frame.

## v4l2loopback buffer count

v4l2loopback gives every reader the **writer's** buffer count, regardless of
how many the reader asked for (asking for 2, 3 or 4 all returned 8), then
delivers frames in all of them. Apps that only set up the count they asked for
end up using buffer indices they never mapped, and crash or show glitches.
Because drivers are allowed to grant *fewer* buffers than requested, the safe
choice is a small count:

```bash
sudo ./set-loopback-buffers.sh 3   # persists in /etc/modprobe.d and reloads the module now
```

(Ubuntu's `v4l2-relayd` 0.2.0 sets 8, which is right for its mmap writer but not for libcamera-loopback.)

## Tools

- `tools/frame-timing.py [device] [frames]` (as your user): reads frames the
  way a typical app does and reports fps, arrival jitter, gaps, duplicates and
  tearing (frames overwritten while held).
- `sudo tools/jitter-diag.py`: stops libcamera-loopback, captures directly from libcamera
  in several variants (GPU/CPU ISP, auto exposure on/off, with and without the
  patch), reports jitter, then restarts libcamera-loopback. Use it to tell libcamera-side
  stutter apart from relay-side stutter.

## Known issues

- **PipeWire loses the camera when libcamera-loopback restarts while you're logged in.**
  Run `systemctl --user restart wireplumber` (the installer does this for you).
- **GNOME Snapshot and Cheese crash at startup** on Ubuntu 24.04 (a
  use-after-free in their GStreamer/PipeWire display paths, right after they
  stop and restart the stream). It also happened with `v4l2-relayd`, so it
  doesn't seem specific to libcamera-loopback. Browsers and V4L2 apps are fine.
- **Colours** from the uncalibrated software ISP are washed out or tinted until
  someone writes a tuning file for your sensor
  (`/opt/libcamera/etc/libcamera/ipa/softisp/<sensor>.yaml`).
- The kernel may log a steady stream of `csi2-N error: Transfer FIFO overflow`
  while streaming on IPU6. It hasn't caused lost frames here.
- x86_64 only: the ioctl structure layouts are hard-coded for it.

## License

libcamera-loopback (`libcamera-loopback.py`, the scripts and the tools) is licensed under the
**GNU Affero General Public License v3.0 or later**; see [LICENSE](LICENSE).

`patches/libcamera-softisp-agc-hysteresis.patch` modifies libcamera and is
therefore licensed under libcamera's **LGPL-2.1-or-later**.

---

libcamera-loopback was developed and debugged with the help of an AI agent; the story of
how it came to be is written up on the author's blog.
