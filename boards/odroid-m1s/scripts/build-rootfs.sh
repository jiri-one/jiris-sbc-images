#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")/.."
# shellcheck source=configs/sources.env
source configs/sources.env
scripts/fetch-bootstrap.sh
if [[ ! -d build/rootfs ]]; then
    mkdir -p build/bootstrap
    podman unshare bsdtar --numeric-owner -xpf "cache/$BOOTSTRAP_NAME" -C build/bootstrap
    podman unshare mv build/bootstrap/root.aarch64 build/rootfs
fi
mkdir -p build/logs
# This container only provides native mount/chroot tools. Target executables
# use the host's registered qemu-aarch64 interpreter (binfmt_misc PF flags).
podman run --rm --privileged --name arch-m1s-rootfs-build \
    --volume "$PWD:/work" \
    docker.io/library/ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3 \
    bash /work/scripts/rootfs-container.sh 2>&1 | tee build/logs/rootfs-build.log
