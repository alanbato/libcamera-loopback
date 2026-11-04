#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alan Velasco
#
# build-deb.sh [debian-revision] — build dist/libcamera-loopback_<version>-<rev>_all.deb
# from this source tree with plain dpkg-deb (no debhelper needed).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/deb"
REV=${1:-1}
UPSTREAM=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/libcamera-loopback.py")
VERSION="$UPSTREAM-$REV"
PKG=libcamera-loopback
OUT="$ROOT/dist"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/$PKG"

grep -q "^$PKG ($VERSION)" "$HERE/changelog" ||
    { echo "packaging/deb/changelog has no entry for $VERSION" >&2; exit 1; }

# --- files ---
install -Dm755 "$ROOT/libcamera-loopback.py"            "$D/usr/lib/$PKG/libcamera-loopback.py"
install -Dm755 "$ROOT/set-loopback-buffers.sh"          "$D/usr/lib/$PKG/set-loopback-buffers.sh"
install -Dm755 "$ROOT/tools/frame-timing.py"            "$D/usr/lib/$PKG/tools/frame-timing.py"
install -Dm755 "$ROOT/tools/jitter-diag.py"             "$D/usr/lib/$PKG/tools/jitter-diag.py"
install -d "$D/usr/lib/systemd/system"
sed 's|/usr/local/lib/libcamera-loopback/|/usr/lib/libcamera-loopback/|' \
    "$ROOT/libcamera-loopback.service" > "$D/usr/lib/systemd/system/$PKG.service"
chmod 644 "$D/usr/lib/systemd/system/$PKG.service"
install -Dm644 "$ROOT/libcamera-loopback.default"       "$D/etc/default/$PKG"
install -Dm644 "$ROOT/packaging/common/modprobe.conf"   "$D/usr/lib/modprobe.d/$PKG.conf"
install -Dm644 "$ROOT/packaging/common/modules-load.conf" "$D/usr/lib/modules-load.d/$PKG.conf"
install -Dm644 "$ROOT/patches/libcamera-softisp-agc-hysteresis.patch" \
                                                        "$D/usr/share/$PKG/patches/libcamera-softisp-agc-hysteresis.patch"
install -Dm644 "$ROOT/README.md"                        "$D/usr/share/doc/$PKG/README.md"
install -Dm644 "$HERE/copyright"                        "$D/usr/share/doc/$PKG/copyright"
gzip -9n -c "$HERE/changelog" > "$D/usr/share/doc/$PKG/changelog.Debian.gz"
install -Dm644 "$HERE/lintian-overrides"                "$D/usr/share/lintian/overrides/$PKG"
chmod 644 "$D/usr/share/doc/$PKG/changelog.Debian.gz"

# --- control area ---
install -d "$D/DEBIAN"
for s in postinst prerm postrm; do install -m755 "$HERE/$s" "$D/DEBIAN/$s"; done
echo "/etc/default/$PKG" > "$D/DEBIAN/conffiles"
SIZE=$(du -sk --exclude=DEBIAN "$D" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$HERE/control.in" > "$D/DEBIAN/control"
(cd "$D" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum) > "$D/DEBIAN/md5sums"
chmod 644 "$D/DEBIAN/control" "$D/DEBIAN/conffiles" "$D/DEBIAN/md5sums"

mkdir -p "$OUT"
dpkg-deb --root-owner-group -Zxz --build "$D" "$OUT/${PKG}_${VERSION}_all.deb"
echo "built $OUT/${PKG}_${VERSION}_all.deb"
