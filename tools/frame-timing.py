#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alan Velasco
"""frame-timing.py [device] [frames] — read frames from a V4L2 capture device the
way a typical app does (REQBUFS 4 / mmap / DQBUF / QBUF) and report arrival
jitter, sequence gaps, duplicate frames and frames that change while being read
(writer overwriting a buffer the reader still holds -> tearing)."""
import hashlib, mmap, os, select, statistics, struct, sys, fcntl, time

DEV = sys.argv[1] if len(sys.argv) > 1 else "/dev/video0"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 300
VIDIOC_REQBUFS, VIDIOC_QUERYBUF = 0xC0145608, 0xC0585609
VIDIOC_QBUF, VIDIOC_DQBUF = 0xC058560F, 0xC0585611
VIDIOC_STREAMON, VIDIOC_STREAMOFF = 0x40045612, 0x40045613
CAP, MMAP = 1, 1
# struct v4l2_buffer (x86_64, 88 bytes): index type bytesused flags field | timeval(16) |
# timecode(16) | sequence memory | m.offset(8, union) | length reserved2 | request_fd/reserved
BUF = "IIIII4xqq16sIIQIII4x"
assert struct.calcsize(BUF) == 88, struct.calcsize(BUF)

def vbuf(index=0):
    return bytearray(struct.pack(BUF, index, CAP, 0, 0, 0, 0, 0, b"", 0, MMAP, 0, 0, 0, 0))

fd = os.open(DEV, os.O_RDWR | os.O_NONBLOCK)
rb = bytearray(struct.pack("5I", 4, CAP, MMAP, 0, 0))
fcntl.ioctl(fd, VIDIOC_REQBUFS, rb)
count = struct.unpack_from("I", rb)[0]
maps = []
for i in range(count):
    b = vbuf(i); fcntl.ioctl(fd, VIDIOC_QUERYBUF, b)
    f = struct.unpack(BUF, b)
    offset, length = f[10], f[11]
    maps.append(mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ, offset=offset))
    fcntl.ioctl(fd, VIDIOC_QBUF, vbuf(i))
fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack("I", CAP))
print(f"granted {count} buffers (asked 4)")

poll = select.poll(); poll.register(fd, select.POLLIN)
times, seqs, hashes, torn, idxs = [], [], [], 0, []
t_end = time.monotonic() + 30
while len(times) < N and time.monotonic() < t_end:
    if not poll.poll(2000):
        print("timeout waiting for frame"); break
    b = vbuf()
    try:
        fcntl.ioctl(fd, VIDIOC_DQBUF, b)
    except BlockingIOError:
        continue
    f = struct.unpack(BUF, b)
    idx, used, seq = f[0], f[2], f[8]
    m = maps[idx]
    h1 = hashlib.blake2b(m[:used:997], digest_size=8).digest()   # sample the frame
    time.sleep(0.010)                                            # "app processing" 10 ms
    h2 = hashlib.blake2b(m[:used:997], digest_size=8).digest()
    if h1 != h2:
        torn += 1
    times.append(time.monotonic()); seqs.append(seq); hashes.append(h1); idxs.append(idx)
    fcntl.ioctl(fd, VIDIOC_QBUF, vbuf(idx))
fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack("I", CAP))

if len(times) < 3:
    sys.exit("too few frames")
iv = [1000 * (b - a) for a, b in zip(times, times[1:])]
dups = sum(1 for a, b in zip(hashes, hashes[1:]) if a == b)
back = sum(1 for a, b in zip(seqs, seqs[1:]) if b <= a)
gaps = sum(b - a - 1 for a, b in zip(seqs, seqs[1:]) if b > a + 1)
print(f"frames {len(times)} in {times[-1]-times[0]:.1f}s -> {(len(times)-1)/(times[-1]-times[0]):.1f} fps")
print(f"interval ms: median {statistics.median(iv):.1f}  p95 {sorted(iv)[int(.95*len(iv))]:.1f}  "
      f"max {max(iv):.1f}  stdev {statistics.pstdev(iv):.1f}")
print(f"intervals >50ms: {sum(1 for x in iv if x > 50)}   <10ms: {sum(1 for x in iv if x < 10)}")
print(f"sequence: gaps={gaps} backwards/repeated={back}   identical consecutive frames={dups}")
print(f"frames changed while held 10 ms (writer overwrote them: tearing): {torn}")
print("buffer index pattern:", "".join(str(i) for i in idxs[:40]))
