#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
source configs/sources.env
scripts/fetch-inputs.sh
if [[ ! -d build/rootfs ]]; then
    mkdir -p build/bootstrap
    podman unshare bsdtar --numeric-owner -xpf "cache/$BOOTSTRAP_NAME" -C build/bootstrap
    podman unshare mv build/bootstrap/root.aarch64 build/rootfs
fi
mkdir -p build/logs
cp scripts/rootfs-container.sh build/rootfs-run.sh
podman exec -i arch-rpi4-tools bash /work/build/rootfs-run.sh 2>&1 | tee build/logs/rootfs-build.log
