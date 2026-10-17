#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
source configs/sources.env
source scripts/build-paths.sh "$@"
export RPI4_REUSE_PACKAGES=0
if [[ -n ${RPI4_ROOTFS_SNAPSHOT:-} ]]; then
    [[ -d $RPI4_ROOTFS_SNAPSHOT && ! -L $RPI4_ROOTFS_SNAPSHOT && ! -e $RPI4_ROOTFS_DIR ]] || {
        echo 'Snapshot must be a directory; the destination rootfs must not already exist.' >&2; exit 1;
    }
    snapshot=$(realpath "$RPI4_ROOTFS_SNAPSHOT")
    [[ -d $snapshot/usr/lib/modules && -f $snapshot/etc/pacman.conf ]] || {
        echo 'Snapshot is not a prepared Arch rootfs.' >&2; exit 1;
    }
    python3 - "$snapshot" "$RPI4_ROOTFS_DIR" <<'PY'
from pathlib import Path
import sys
source, destination = (Path(value).resolve() for value in sys.argv[1:])
if (not destination.is_relative_to(Path.cwd().resolve()) or
        destination.is_relative_to(source) or source.is_relative_to(destination)):
    raise SystemExit('Snapshot and destination must be separate; destination must remain inside the project.')
PY
    while IFS= read -r mounted; do
        case $mounted in "$snapshot"|"$snapshot"/*) echo 'Unmount the rootfs snapshot before cloning it.' >&2; exit 1 ;; esac
    done < <(findmnt -rn -o TARGET)
    mkdir -p "$RPI4_BUILD_DIR"
    podman unshare cp -a --reflink=auto -- "$snapshot" "$RPI4_ROOTFS_DIR"
    export RPI4_REUSE_PACKAGES=1
fi
scripts/fetch-inputs.sh
if [[ ! -d $RPI4_ROOTFS_DIR ]]; then
    mkdir -p "$RPI4_BUILD_DIR/bootstrap"
    podman unshare bsdtar --numeric-owner -xpf "cache/$BOOTSTRAP_NAME" -C "$RPI4_BUILD_DIR/bootstrap"
    podman unshare mv "$RPI4_BUILD_DIR/bootstrap/root.aarch64" "$RPI4_ROOTFS_DIR"
fi
mkdir -p "$RPI4_BUILD_DIR/logs"
cp scripts/rootfs-container.sh "$RPI4_BUILD_DIR/rootfs-run.sh"
podman exec -i --env RPI4_BOOT_MODE --env RPI4_REUSE_PACKAGES arch-rpi4-tools \
    bash "/work/$RPI4_BUILD_DIR/rootfs-run.sh" 2>&1 | tee "$RPI4_BUILD_DIR/logs/rootfs-build.log"
